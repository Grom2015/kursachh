import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.db.init_db import init_db
from app.db.models import Company
from app.db.session import SessionLocal

STATUSES = ["AUTO_READY", "AUTO_PARTIAL", "SOURCE_BLOCKED", "PARSER_BLOCKED", "LOW_CONFIDENCE", "UNSUPPORTED"]


def scan_auto_support_coverage(
    period_from: str,
    period_to: str,
    reporting_standard: str = "IFRS",
    from_registry: bool = True,
) -> dict[str, Any]:
    init_db()
    with SessionLocal() as db:
        companies = db.scalars(select(Company).where(Company.is_active.is_(True)).order_by(Company.ticker)).all()
    rows = []
    for company in companies if from_registry else []:
        try:
            rows.append(company_row(company, period_from, period_to, reporting_standard))
        except Exception as exc:
            rows.append(
                {
                    "ticker": company.ticker,
                    "company": company.full_name,
                    "period_from": period_from,
                    "period_to": period_to,
                    "reporting_standard": reporting_standard,
                    "automated_support_status": "UNSUPPORTED",
                    "automated_quality_score": 0.0,
                    "manual_action_required": False,
                    "blockers": [f"Coverage scan failed for company: {exc}"],
                    "warnings": [],
                    "supported_outputs": {
                        "financial_facts": False,
                        "financial_metrics": False,
                        "market_analysis": False,
                        "peer_comparison": False,
                        "llm_payload": False,
                    },
                }
            )
    counts = Counter(row["automated_support_status"] for row in rows)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "period_from": period_from,
        "period_to": period_to,
        "reporting_standard": reporting_standard,
        "companies_scanned": len(rows),
        "status_counts": {status: counts.get(status, 0) for status in STATUSES},
        "companies": rows,
        "top_blockers": top_blockers(rows),
        "warnings": [],
    }
    save_report(report, period_from)
    return report


def company_row(company: Company, period_from: str, period_to: str, reporting_standard: str) -> dict[str, Any]:
    validation = load_validation_report(company.ticker, period_from, period_to)
    auto = validation.get("automated_support_status") or {}
    status = auto.get("status") or fallback_status(validation)
    return {
        "ticker": company.ticker,
        "company": company.full_name,
        "period_from": period_from,
        "period_to": period_to,
        "reporting_standard": reporting_standard,
        "automated_support_status": status,
        "automated_quality_score": auto.get("automated_quality_score", 0.0),
        "manual_action_required": False,
        "blockers": auto.get("blockers") or validation.get("warnings") or ["Automated support report not available."],
        "warnings": auto.get("warnings") or [],
        "supported_outputs": auto.get(
            "supported_outputs",
            {
                "financial_facts": False,
                "financial_metrics": False,
                "market_analysis": False,
                "peer_comparison": False,
                "llm_payload": False,
            },
        ),
        "supported_output_scopes": auto.get("supported_output_scopes", []),
        "unsupported_output_scopes": auto.get("unsupported_output_scopes", []),
        "scope_statuses": auto.get("scope_statuses", {}),
    }


def load_validation_report(ticker: str, period_from: str, period_to: str) -> dict[str, Any]:
    path = validation_report_path(ticker, period_from, period_to)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def validation_report_path(ticker: str, period_from: str, period_to: str) -> Path:
    return (
        get_settings().root_dir
        / "data"
        / "validation"
        / ticker.upper()
        / f"{period_from}_{period_to}_real_validation_report.json"
    )


def fallback_status(validation: dict[str, Any]) -> str:
    if not validation:
        return "UNSUPPORTED"
    summary = validation.get("summary") or {}
    if not summary.get("source_document_count"):
        return "SOURCE_BLOCKED"
    if not summary.get("canonical_facts_count"):
        return "PARSER_BLOCKED"
    if summary.get("conflicting_fact_count") or not summary.get("high_confidence_fact_count"):
        return "LOW_CONFIDENCE"
    return "AUTO_PARTIAL"


def top_blockers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter = Counter(blocker for row in rows for blocker in row.get("blockers", []))
    return [{"blocker": blocker, "count": count} for blocker, count in counter.most_common(10)]


def save_report(report: dict[str, Any], period_from: str) -> Path:
    year = period_from[:4]
    root = get_settings().root_dir / "data" / "validation" / "auto_support_coverage"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"moex_auto_support_coverage_{year}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan automated real-data support coverage across the company registry.")
    parser.add_argument("--from-registry", action="store_true")
    parser.add_argument("--period-from", required=True)
    parser.add_argument("--period-to", required=True)
    parser.add_argument("--reporting-standard", default="IFRS")
    args = parser.parse_args(argv)
    report = scan_auto_support_coverage(
        period_from=args.period_from,
        period_to=args.period_to,
        reporting_standard=args.reporting_standard,
        from_registry=args.from_registry,
    )
    path = save_report(report, args.period_from)
    print(
        "\n".join(
            [
                f"companies_scanned: {report['companies_scanned']}",
                f"AUTO_READY: {report['status_counts']['AUTO_READY']}",
                f"AUTO_PARTIAL: {report['status_counts']['AUTO_PARTIAL']}",
                f"SOURCE_BLOCKED: {report['status_counts']['SOURCE_BLOCKED']}",
                f"PARSER_BLOCKED: {report['status_counts']['PARSER_BLOCKED']}",
                f"LOW_CONFIDENCE: {report['status_counts']['LOW_CONFIDENCE']}",
                f"UNSUPPORTED: {report['status_counts']['UNSUPPORTED']}",
                f"report_path: {path}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
