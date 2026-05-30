import argparse
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings
from app.services.reports.tls import (
    TLSConfigError,
    certificate_metadata,
    classify_httpx_failure,
    runtime_tls_metadata,
    tls_report_fields,
    tls_verify_config,
)


def allowed_domain(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.hostname in set(get_settings().report_source_allowed_domains)


def diagnose_url(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    report: dict[str, Any] = {
        "url": url,
        "hostname": parsed.hostname,
        "trusted_domain": allowed_domain(url),
        "status": "FAIL",
        "failure_reason": None,
        "http_status_code": None,
        "content_type": None,
        "exception_type": None,
        "exception_message": None,
        **runtime_tls_metadata(),
    }
    try:
        report.update(tls_report_fields())
    except TLSConfigError as exc:
        report["failure_reason"] = "tls_config_error"
        report["exception_type"] = type(exc).__name__
        report["exception_message"] = str(exc)
        report["certificate"] = certificate_metadata(url)
        return report
    if not report["trusted_domain"]:
        report["failure_reason"] = "non_allowlisted_domain"
        report["certificate"] = certificate_metadata(url)
        return report
    try:
        tls_config = tls_verify_config()
        with httpx.Client(timeout=15, follow_redirects=True, verify=tls_config.verify) as client:
            response = client.head(url)
            if response.status_code >= 400:
                response = client.get(url)
            response.raise_for_status()
        report["status"] = "PASS"
        report["http_status_code"] = response.status_code
        report["content_type"] = response.headers.get("content-type")
    except Exception as exc:
        report["failure_reason"] = classify_httpx_failure(exc)
        report["exception_type"] = type(exc).__name__
        report["exception_message"] = str(exc)
    report["certificate"] = certificate_metadata(url)
    return report


def save_report(report: dict[str, Any]) -> Path:
    hostname = (report.get("hostname") or "source").replace(".", "_")
    root = get_settings().root_dir / "data" / "validation" / "TLS"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{hostname}_tls_diagnostics.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose TLS verification for an allowlisted report source URL.")
    parser.add_argument("url")
    args = parser.parse_args(argv)
    report = diagnose_url(args.url)
    path = save_report(report)
    print(
        "\n".join(
            [
                f"url: {report['url']}",
                f"hostname: {report['hostname']}",
                f"trusted_domain: {report['trusted_domain']}",
                f"status: {report['status']}",
                f"failure_reason: {report['failure_reason']}",
                f"tls_verify_mode: {report.get('tls_verify_mode')}",
                f"ca_bundle_path_used: {report.get('ca_bundle_path_used')}",
                f"certifi_path: {report.get('certifi_path')}",
                f"exception_type: {report.get('exception_type')}",
                f"report_path: {path}",
            ]
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
