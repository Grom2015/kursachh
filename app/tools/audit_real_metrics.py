import argparse
import json
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
from app.tools.verify_golden_facts import verify as verify_golden_facts


def run_audit(ticker: str, period_from: str, period_to: str) -> dict[str, Any]:
    ticker = ticker.upper()
    with SessionLocal() as db:
        company = db.scalar(select(Company).where(Company.ticker == ticker))
        if not company:
            raise RuntimeError(f"{ticker} is not seeded")
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
            report = empty_report(ticker, period_from, period_to, [f"No fresh {ticker} real AnalysisResult found."])
            path = save_report(report)
            print_summary(report, path)
            return report
        periods = periods_between(period_from, period_to)
        facts = db.scalars(
            select(StatementFact).where(StatementFact.company_id == company.id, StatementFact.period.in_(periods))
        ).all()
    metrics = result.result_json.get("financial_analysis", {}).get("metrics", [])
    audited = audit_metric_rows(metrics, facts)
    apply_issuer_metric_methodology_notes(ticker, audited)
    counts = metric_status_counts(audited)
    trust = metric_data_trust(ticker, period_from, period_to)
    report = {
        "company": ticker,
        "period_from": period_from,
        "period_to": period_to,
        "data_mode": "real",
        "source": "db_analysis_result",
        "audit_input_freshness": "fresh_db_result",
        "metrics_total": len(audited),
        "metrics_calculated": sum(1 for row in audited if row["value"] is not None),
        "metrics_missing": counts["metrics_missing_count"],
        "metrics": audited,
        "questionable_metrics_breakdown": questionable_metrics_breakdown(audited),
        "missing_metrics_breakdown": missing_metrics_breakdown(audited),
        "metric_decision_table": metric_decision_table(audited),
        "metric_data_trust": trust,
        "summary": counts,
        "warnings": sorted({warning for row in audited for warning in row["methodology_warnings"]}),
        "next_actions": next_actions(counts),
    }
    path = save_report(report)
    print_summary(report, path)
    return report


def empty_report(ticker: str, period_from: str, period_to: str, warnings: list[str]) -> dict[str, Any]:
    counts = {
        "metrics_valid_count": 0,
        "metrics_questionable_count": 0,
        "metrics_invalid_count": 0,
        "metrics_missing_count": 0,
    }
    return {
        "company": ticker,
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
        "metric_data_trust": {
            "status": "unverified",
            "review_pack_verified": 0,
            "review_pack_total": 0,
            "warning": "No metric source facts have been manually verified.",
        },
        "summary": counts,
        "warnings": warnings,
        "next_actions": ["Run replay-cache validation or live validation before auditing real metrics."],
    }


def next_actions(counts: dict[str, int]) -> list[str]:
    actions = []
    if counts["metrics_invalid_count"]:
        actions.append("Review invalid metrics and block them from analytical payloads.")
    if counts["metrics_questionable_count"]:
        actions.append("Review questionable metrics before peer comparison.")
    if counts["metrics_missing_count"]:
        actions.append("Add missing canonical facts or valuation inputs before expanding metric coverage.")
    return actions


def metric_data_trust(ticker: str, period_from: str, period_to: str) -> dict[str, Any]:
    golden = verify_golden_facts(ticker, period_from, period_to)
    verified = golden.get("review_pack_verified", 0) or 0
    total = golden.get("review_pack_total", 0) or 0
    status = "unverified"
    warning = "Metric inputs have not been manually verified."
    if golden.get("review_pack_status") == "PASS":
        status = "verified_review_pack"
        warning = "Review-pack facts are manually verified; remaining non-reviewed facts are not fully golden-covered."
    elif verified:
        status = "partially_verified"
        warning = "Only part of the review-pack facts are manually verified."
    return {
        "status": status,
        "review_pack_verified": verified,
        "review_pack_total": total,
        "review_pack_accuracy": golden.get("review_pack_accuracy"),
        "warning": warning,
    }


def apply_issuer_metric_methodology_notes(ticker: str, rows: list[dict[str, Any]]) -> None:
    if ticker.upper() != "GAZP":
        return
    warning = "capex uses reported segment capital expenditures, not cash-flow purchase of PPE."
    for row in rows:
        inputs = row.get("inputs") or {}
        if row.get("metric_code") in {"fcf", "fcf_margin"} or "capex" in inputs:
            warnings = row.setdefault("methodology_warnings", [])
            if warning not in warnings:
                warnings.append(warning)


def save_report(report: dict[str, Any]) -> Path:
    root = get_settings().root_dir / "data" / "validation" / report["company"].upper()
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
                f"metrics_total: {report['metrics_total']}",
                f"metrics_calculated: {report['metrics_calculated']}",
                f"metrics_valid: {summary['metrics_valid_count']}",
                f"metrics_questionable: {summary['metrics_questionable_count']}",
                f"metrics_invalid: {summary['metrics_invalid_count']}",
                f"metrics_missing: {summary['metrics_missing_count']}",
                f"report_path: {path}",
            ]
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit real metric methodology for a ticker.")
    parser.add_argument("ticker")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    parser.add_argument("--from-db", action="store_true")
    args = parser.parse_args(argv)
    run_audit(args.ticker, args.period_from, args.period_to)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
