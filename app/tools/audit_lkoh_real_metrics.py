import argparse
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select

from app.core.config import get_settings
from app.db.models import AnalysisResult, Company, StatementFact
from app.db.session import SessionLocal
from app.services.metrics.metric_audit import (
    audit_metric_rows,
    metric_decision_table,
    metric_status_counts,
    missing_metrics_breakdown,
    questionable_metrics_breakdown,
)
from app.services.periods import periods_between


def run_audit(period_from: str, period_to: str, source_mode: str = "latest") -> dict[str, Any]:
    if source_mode == "from-validation-report":
        report = run_audit_from_validation_json(period_from, period_to)
        path = save_report(report)
        print("Audit is based on existing validation report, not a fresh parser run.")
        print_summary(report, path)
        return report
    with SessionLocal() as db:
        company = db.scalar(select(Company).where(Company.ticker == "LKOH"))
        if not company:
            raise RuntimeError("LKOH is not seeded")
        result = db.scalar(
            select(AnalysisResult)
            .where(
                AnalysisResult.company_id == company.id,
                AnalysisResult.period_from == period_from,
                AnalysisResult.period_to == period_to,
            )
            .order_by(desc(AnalysisResult.created_at))
        )
        if not result:
            if source_mode == "from-db":
                report = stale_or_missing_report(
                    period_from,
                    period_to,
                    ["No fresh LKOH real AnalysisResult found in DB for requested period."],
                )
                path = save_report(report)
                print_summary(report, path)
                return report
            report = run_audit_from_validation_json(period_from, period_to)
            path = save_report(report)
            print("Audit is based on existing validation report, not a fresh parser run.")
            print_summary(report, path)
            return report
        periods = periods_between(period_from, period_to)
        facts = db.scalars(
            select(StatementFact).where(StatementFact.company_id == company.id, StatementFact.period.in_(periods))
        ).all()
        metrics = result.result_json.get("financial_analysis", {}).get("metrics", [])
        audited = audit_metric_rows(metrics, facts)
        counts = metric_status_counts(audited)
        questionable = questionable_metrics_breakdown(audited)
        missing = missing_metrics_breakdown(audited)
        decisions = metric_decision_table(audited)
        report = {
            "company": "LKOH",
            "period_from": period_from,
            "period_to": period_to,
            "data_mode": result.data_snapshot_json.get("data_mode", "real"),
            "source": "db_analysis_result",
            "audit_input_freshness": "fresh_db_result",
            "metrics_total": len(audited),
            "metrics_calculated": sum(1 for row in audited if row["value"] is not None),
            "metrics_missing": counts["metrics_missing_count"],
            "metrics": audited,
            "questionable_metrics_breakdown": questionable,
            "missing_metrics_breakdown": missing,
            "metric_decision_table": decisions,
            "summary": counts,
            "warnings": sorted({warning for row in audited for warning in row["methodology_warnings"]}),
            "next_actions": next_actions(counts),
        }
        path = save_report(report)
        print_summary(report, path)
        return report


def run_audit_from_validation_json(period_from: str, period_to: str) -> dict[str, Any]:
    path = get_settings().root_dir / "data" / "validation" / "LKOH" / f"{period_from}_{period_to}_real_validation_report.json"
    if not path.exists():
        return stale_or_missing_report(
            period_from,
            period_to,
            ["No LKOH real analysis result or validation JSON found; run validate_lkoh_real_extraction first."],
        )
    validation = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for metric in validation.get("metric_coverage", []):
        status = "missing" if metric.get("quality_flag") == "missing" else "questionable"
        if metric.get("value") is not None and not metric.get("warnings"):
            status = "questionable"
        rows.append(
            {
                "metric_code": metric.get("metric_code"),
                "period": metric.get("period"),
                "status": status,
                "value": metric.get("value"),
                "quality_flag": metric.get("quality_flag"),
                "formula": metric.get("formula"),
                "inputs": metric.get("inputs") or {},
                "input_period_types": {},
                "methodology_warnings": metric.get("warnings") or ["Validation JSON fallback; DB input facts unavailable"],
                "source_locations": [],
            }
        )
    counts = metric_status_counts(rows)
    questionable = questionable_metrics_breakdown(rows)
    missing = missing_metrics_breakdown(rows)
    decisions = metric_decision_table(rows)
    return {
        "company": "LKOH",
        "period_from": period_from,
        "period_to": period_to,
        "data_mode": "real",
        "source": "validation_report_fallback",
        "audit_input_freshness": "validation_report_fallback",
        "metrics_total": len(rows),
        "metrics_calculated": sum(1 for row in rows if row["value"] is not None),
        "metrics_missing": counts["metrics_missing_count"],
        "metrics": rows,
        "questionable_metrics_breakdown": questionable,
        "missing_metrics_breakdown": missing,
        "metric_decision_table": decisions,
        "summary": counts,
        "warnings": sorted({warning for row in rows for warning in row["methodology_warnings"]}),
        "next_actions": next_actions(counts),
    }


def stale_or_missing_report(period_from: str, period_to: str, warnings: list[str]) -> dict[str, Any]:
    counts = {
        "metrics_valid_count": 0,
        "metrics_questionable_count": 0,
        "metrics_invalid_count": 0,
        "metrics_missing_count": 0,
    }
    return {
        "company": "LKOH",
        "period_from": period_from,
        "period_to": period_to,
        "data_mode": "real",
        "source": "none",
        "audit_input_freshness": "stale_or_missing",
        "metrics_total": 0,
        "metrics_calculated": 0,
        "metrics_missing": 0,
        "metrics": [],
        "questionable_metrics_breakdown": [],
        "missing_metrics_breakdown": {
            "fixable_by_parser": [],
            "unavailable_in_source": [],
            "requires_market_inputs": [],
            "requires_explicit_disclosure": [],
            "blocked_by_period_semantics": [],
        },
        "metric_decision_table": [],
        "summary": counts,
        "warnings": warnings,
        "next_actions": ["Run replay-cache validation or live validation before auditing real metrics."],
    }


def next_actions(counts: dict[str, int]) -> list[str]:
    actions = []
    if counts["metrics_invalid_count"]:
        actions.append("Review invalid metrics and block them from analytical payloads until methodology is fixed.")
    if counts["metrics_questionable_count"]:
        actions.append("Review questionable metrics, especially YTD-based growth and ROE/ROA annualization.")
    if counts["metrics_missing_count"]:
        actions.append("Add missing canonical facts or valuation inputs before expanding metric coverage.")
    return actions


def save_report(report: dict[str, Any]) -> Path:
    root = get_settings().root_dir / "data" / "validation" / "LKOH"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{report['period_from']}_{report['period_to']}_real_metrics_audit.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def print_summary(report: dict[str, Any], path: Path) -> None:
    summary = report["summary"]
    print(
        "\n".join(
            [
                f"company: {report['company']}",
                f"period: {report['period_from']}..{report['period_to']}",
                f"source: {report.get('source')}",
                f"audit_input_freshness: {report.get('audit_input_freshness')}",
                f"metrics_total: {report['metrics_total']}",
                f"metrics_calculated: {report['metrics_calculated']}",
                f"metrics_missing: {report['metrics_missing']}",
                f"metrics_valid: {summary['metrics_valid_count']}",
                f"metrics_questionable: {summary['metrics_questionable_count']}",
                f"metrics_invalid: {summary['metrics_invalid_count']}",
                f"report_path: {path}",
            ]
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit LKOH real metric methodology.")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--from-validation-report", action="store_true")
    group.add_argument("--from-db", action="store_true")
    group.add_argument("--latest", action="store_true")
    args = parser.parse_args(argv or sys.argv[1:])
    mode = "latest"
    if args.from_validation_report:
        mode = "from-validation-report"
    elif args.from_db:
        mode = "from-db"
    run_audit(args.period_from, args.period_to, source_mode=mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
