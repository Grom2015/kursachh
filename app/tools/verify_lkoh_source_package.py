import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

from app.core.config import get_settings
from app.services.periods import periods_between
from app.services.reports.tls import (
    TLSConfigError,
    classify_httpx_failure,
    tls_report_fields,
    tls_verify_config,
)
from app.tools.diagnose_tls_source import diagnose_url
from app.tools.diagnose_tls_source import save_report as save_tls_diagnostics_report

EXPECTED_FILE_TYPES = {"pdf", "xlsx", "zip", "html", "unknown"}
SOURCE_ROLES = ["press_release", "financial_statements", "financial_supplement", "annual_report", "other"]
PROPER_SOURCE_ROLES = {"financial_statements", "financial_supplement", "annual_report"}


class SourcePackageVerifier:
    def __init__(self, ticker: str | Path = "LKOH", root: Path | None = None):
        if isinstance(ticker, Path):
            self.ticker = "LKOH"
            self.root = ticker
        else:
            self.ticker = ticker.upper()
            self.root = root or get_settings().root_dir

    def verify(self, period_from: str, period_to: str, live: bool = False, tls_diagnostics: bool = False) -> dict[str, Any]:
        periods = periods_between(period_from, period_to)
        manifest_reports = self._load_manifest_reports()
        period_rows = []
        warnings: list[str] = []
        for period in periods:
            sources = [item for item in manifest_reports if item.get("period") == period]
            source_rows = [self._source_payload(item, live, tls_diagnostics) for item in sources]
            for row in source_rows:
                warnings.extend(row["warnings"])
            has_financial_statements = any(row["source_role"] == "financial_statements" for row in source_rows)
            has_financial_supplement = any(row["source_role"] == "financial_supplement" for row in source_rows)
            has_annual_report = any(row["source_role"] == "annual_report" for row in source_rows)
            has_verified_financial_statements = any(
                row["source_role"] == "financial_statements" and row["source_verified"] for row in source_rows
            )
            has_verified_financial_supplement = any(
                row["source_role"] == "financial_supplement" and row["source_verified"] for row in source_rows
            )
            has_verified_annual_report = any(
                row["source_role"] == "annual_report" and row["source_verified"] for row in source_rows
            )
            readiness = (
                "ready"
                if has_financial_statements or has_financial_supplement or has_annual_report
                else "partial"
                if source_rows
                else "missing"
            )
            period_rows.append(
                {
                    "period": period,
                    "sources": source_rows,
                    "has_financial_statements": has_financial_statements,
                    "has_financial_supplement": has_financial_supplement,
                    "has_annual_report": has_annual_report,
                    "has_verified_financial_statements": has_verified_financial_statements,
                    "has_verified_financial_supplement": has_verified_financial_supplement,
                    "has_verified_annual_report": has_verified_annual_report,
                    "readiness": readiness,
                }
            )
        summary = self._summary(period_rows)
        status = self._status(summary, len(periods), live=live)
        warnings.extend(self._package_warnings(summary, status))
        report = {
            "company": self.ticker,
            "period_from": period_from,
            "period_to": period_to,
            "status": status,
            "periods": period_rows,
            "summary": summary,
            "warnings": sorted(set(warnings)),
            "next_actions": self._next_actions(status, summary),
            **tls_report_fields(),
        }
        blocker = self._unresolved_blocker(summary)
        if blocker:
            report["unresolved_blocker"] = blocker
            report["recommended_action"] = ["manual source acquisition required", "switch peer candidate"]
        return report

    def _load_manifest_reports(self) -> list[dict[str, Any]]:
        path = self.root / "data" / "manifests" / f"{self.ticker.casefold()}_real_sources.yml"
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data.get("reports", []) or []

    def _source_payload(self, item: dict[str, Any], live: bool, tls_diagnostics: bool = False) -> dict[str, Any]:
        source_url = item.get("source_url", "")
        expected_file_type = item.get("expected_file_type", "unknown")
        warnings = []
        trusted_domain = self._trusted_domain(source_url)
        if not trusted_domain:
            warnings.append(f"Source URL domain is not allowlisted: {source_url}")
        if expected_file_type not in EXPECTED_FILE_TYPES:
            warnings.append(f"Invalid expected_file_type: {expected_file_type}")
        path = urlparse(source_url).path.casefold()
        if expected_file_type == "pdf" and path.endswith((".html", ".htm")):
            warnings.append("HTML-looking URL is marked as pdf")
        content_type = None
        failure_reason = None
        tls_diagnostics_path = None
        if live:
            content_type, live_warning, failure_reason = self._live_content_type(source_url)
            if live_warning:
                warnings.append(live_warning)
            if tls_diagnostics and failure_reason == "tls_certificate_verify_failed":
                tls_report = diagnose_url(source_url)
                tls_diagnostics_path = str(save_tls_diagnostics_report(tls_report))
            if content_type and expected_file_type in {"pdf", "xlsx", "zip", "html"}:
                if not self._content_type_matches(content_type, expected_file_type):
                    warnings.append(f"Live content-type does not match expected_file_type: {content_type}")
        source_verified = bool(trusted_domain) and not warnings if live else bool(trusted_domain)
        return {
            "source_role": item.get("source_role") or item.get("document_type", "other"),
            "source_url": source_url,
            "expected_file_type": expected_file_type,
            "trusted_domain": bool(trusted_domain),
            "source_verified": source_verified,
            "failure_reason": failure_reason,
            "tls_diagnostics_path": tls_diagnostics_path,
            "live_checked": live,
            "content_type": content_type,
            "warnings": warnings,
        }

    def _trusted_domain(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and parsed.hostname in set(get_settings().report_source_allowed_domains)

    def _live_content_type(self, url: str) -> tuple[str | None, str | None, str | None]:
        try:
            tls_config = tls_verify_config()
            with httpx.Client(timeout=15, follow_redirects=True, verify=tls_config.verify) as client:
                response = client.head(url)
                if response.status_code >= 400:
                    response = client.get(url)
                response.raise_for_status()
                return response.headers.get("content-type"), None, None
        except TLSConfigError as exc:
            return None, f"TLS configuration error for {url}: {exc}", "tls_config_error"
        except Exception as exc:
            failure_reason = classify_httpx_failure(exc)
            return None, f"Live metadata check failed for {url}: {failure_reason}: {exc}", failure_reason

    def _content_type_matches(self, content_type: str, expected_file_type: str) -> bool:
        normalized = content_type.casefold()
        tokens = {
            "pdf": ["pdf", "octet-stream"],
            "xlsx": ["spreadsheet", "excel", "octet-stream"],
            "zip": ["zip", "octet-stream"],
            "html": ["html"],
        }
        return any(token in normalized for token in tokens[expected_file_type])

    def _summary(self, periods: list[dict[str, Any]]) -> dict[str, int]:
        periods_with_financial_statements = sum(1 for period in periods if period["has_financial_statements"])
        periods_with_financial_supplement = sum(1 for period in periods if period["has_financial_supplement"])
        periods_with_annual_report = sum(1 for period in periods if period["has_annual_report"])
        periods_with_verified_financial_statements = sum(
            1 for period in periods if period["has_verified_financial_statements"]
        )
        periods_with_verified_financial_supplement = sum(
            1 for period in periods if period["has_verified_financial_supplement"]
        )
        periods_with_verified_annual_report = sum(1 for period in periods if period["has_verified_annual_report"])
        periods_only_press_release = sum(
            1
            for period in periods
            if period["sources"]
            and all(source["source_role"] == "press_release" for source in period["sources"])
        )
        source_role_summary = {
            role: sum(1 for period in periods for source in period["sources"] if source["source_role"] == role)
            for role in SOURCE_ROLES
        }
        return {
            "period_count": len(periods),
            "periods_with_financial_statements": periods_with_financial_statements,
            "periods_with_financial_supplement": periods_with_financial_supplement,
            "periods_with_annual_report": periods_with_annual_report,
            "periods_with_verified_financial_statements": periods_with_verified_financial_statements,
            "periods_with_verified_financial_supplement": periods_with_verified_financial_supplement,
            "periods_with_verified_annual_report": periods_with_verified_annual_report,
            "periods_only_press_release": periods_only_press_release,
            "missing_periods": [period["period"] for period in periods if not period["sources"]],
            **{f"source_role_{role}_count": count for role, count in source_role_summary.items()},
        }

    def _status(self, summary: dict[str, int], period_count: int, live: bool = False) -> str:
        prefix = "periods_with_verified_" if live else "periods_with_"
        periods_with_proper_source = (
            summary[f"{prefix}financial_statements"] + summary[f"{prefix}financial_supplement"]
            + summary[f"{prefix}annual_report"]
        )
        if periods_with_proper_source >= period_count:
            return "READY"
        if periods_with_proper_source:
            return "PARTIAL"
        return "NOT_READY"

    def _package_warnings(self, summary: dict[str, int], status: str) -> list[str]:
        if status == "NOT_READY" and summary["periods_only_press_release"]:
            return ["Only press release sources found; add full financial statements or financial supplements."]
        return []

    def _next_actions(self, status: str, summary: dict[str, int]) -> list[str]:
        actions = []
        if status != "READY":
            actions.append(
                f"Add verified {self.ticker} IFRS financial_statements PDF or financial_supplement XLSX URLs for each period."
            )
        if summary["periods_only_press_release"]:
            actions.append("Keep press releases as supporting sources, but do not use them as full statement packages.")
        return actions

    def _unresolved_blocker(self, summary: dict[str, int]) -> str | None:
        if self.ticker == "TATN" and "2021Q4" in set(summary.get("missing_periods") or []):
            return "TATN FY/12M 2021 official IFRS source not verified"
        return None


def save_source_package_report(report: dict[str, Any]) -> Path:
    root = get_settings().root_dir / "data" / "validation" / report["company"].upper()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{report['period_from']}_{report['period_to']}_source_package_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def print_summary(report: dict[str, Any], path: Path) -> None:
    summary = report["summary"]
    lines = [
        f"company: {report['company']}",
        f"period: {report['period_from']}..{report['period_to']}",
        f"status: {report['status']}",
        f"period_count: {summary['period_count']}",
        f"periods_with_financial_statements: {summary['periods_with_financial_statements']}",
        f"periods_with_verified_financial_statements: {summary['periods_with_verified_financial_statements']}",
        f"periods_with_financial_supplement: {summary['periods_with_financial_supplement']}",
        f"periods_with_verified_financial_supplement: {summary['periods_with_verified_financial_supplement']}",
        f"periods_with_annual_report: {summary['periods_with_annual_report']}",
        f"periods_with_verified_annual_report: {summary['periods_with_verified_annual_report']}",
        f"periods_only_press_release: {summary['periods_only_press_release']}",
        f"missing_periods: {', '.join(summary['missing_periods'])}",
        f"report_path: {path}",
    ]
    print("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    parser.add_argument("--live", action="store_true", help="Check live content-type metadata with timeout.")
    parser.add_argument("--tls-diagnostics", action="store_true", help="Write TLS diagnostics if live TLS verification fails.")
    args = parser.parse_args(argv or sys.argv[1:])
    report = SourcePackageVerifier("LKOH").verify(
        args.period_from, args.period_to, live=args.live, tls_diagnostics=args.tls_diagnostics
    )
    path = save_source_package_report(report)
    print_summary(report, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
