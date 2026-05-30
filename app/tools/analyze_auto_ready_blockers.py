import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import get_settings


def analyze(tickers: list[str], period_from: str, period_to: str) -> dict[str, Any]:
    companies = [ticker.upper() for ticker in tickers]
    rows = [company_analysis(ticker, period_from, period_to) for ticker in companies]
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "companies": companies,
        "period_from": period_from,
        "period_to": period_to,
        "company_results": rows,
        "best_next_engineering_action": best_next_action(rows),
        "warnings": [
            "AUTO_READY is scoped. Top-level status must not be interpreted as full company analysis readiness.",
            "Unsupported valuation/EBITDA metrics remain visible but are excluded from statement-based denominator.",
        ],
    }
    save_report(report, period_from)
    return report


def company_analysis(ticker: str, period_from: str, period_to: str) -> dict[str, Any]:
    report = load_validation_report(ticker, period_from, period_to)
    auto = report.get("automated_support_status") or {}
    scope = (auto.get("scope_statuses") or {}).get("statement_based_financials", {})
    summary = report.get("summary") or {}
    unsupported = scope.get("unsupported_by_policy") or []
    missing_expected = scope.get("missing_but_expected") or []
    missing_facts = missing_expected_facts_from_metrics(missing_expected)
    return {
        "ticker": ticker,
        "old_top_level_status": auto.get("status") or fallback_status(report),
        "new_statement_scope_status": scope.get("status", "UNSUPPORTED"),
        "old_metric_quality_score": legacy_metric_quality_score(summary),
        "new_statement_based_financials_score": scope.get("score", 0.0),
        "old_automated_quality_score": auto.get("automated_quality_score", 0.0),
        "canonical_facts_count": summary.get("canonical_facts_count", 0),
        "metrics_valid_count": summary.get("metrics_valid_count", 0),
        "metrics_questionable_count": summary.get("metrics_questionable_count", 0),
        "metrics_missing_count": summary.get("metrics_missing_count", 0),
        "unsupported_by_policy_metrics": unsupported,
        "unsupported_by_policy_count": len(unsupported),
        "missing_but_expected_metrics": missing_expected,
        "missing_but_expected_facts": missing_facts,
        "methodology_sensitive_metrics": scope.get("methodology_sensitive") or [],
        "recommended_next_action": recommended_action(ticker, auto, scope, missing_facts),
    }


def missing_expected_facts_from_metrics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        period = row.get("period") or "unknown"
        for fact in row.get("required_facts") or []:
            grouped[(fact, row.get("metric_code", "unknown"))].add(period)
    return [
        {
            "fact_code": fact,
            "metric_code": metric,
            "periods": sorted(periods),
        }
        for (fact, metric), periods in sorted(grouped.items())
    ]


def recommended_action(ticker: str, auto: dict[str, Any], scope: dict[str, Any], missing_facts: list[dict[str, Any]]) -> str:
    if auto.get("source_package_status") != "READY":
        return f"Resolve {ticker} source package coverage before parser or metric work."
    if scope.get("status") == "AUTO_READY":
        return f"{ticker} statement-based scope is ready; next work should target other scopes only if product needs them."
    current_ratio_facts = {"current_assets", "current_liabilities"}
    missing_codes = {row["fact_code"] for row in missing_facts}
    if current_ratio_facts <= missing_codes:
        return f"Inspect {ticker} balance sheet extraction for current_assets/current_liabilities."
    if {"operating_profit", "net_income"} & missing_codes:
        return f"Inspect {ticker} P&L extraction for operating_profit/net_income."
    if {"operating_cash_flow", "capex"} & missing_codes:
        return f"Inspect {ticker} cash flow/capex extraction and preserve methodology warnings."
    return f"Review {ticker} missing expected statement inputs before expanding scope."


def legacy_metric_quality_score(summary: dict[str, Any]) -> float:
    valid = summary.get("metrics_valid_count", 0) or 0
    questionable = summary.get("metrics_questionable_count", 0) or 0
    missing = summary.get("metrics_missing_count", 0) or 0
    invalid = summary.get("metrics_invalid_count", 0) or 0
    total = valid + questionable + missing + invalid
    return round(valid / total, 4) if total else 0.0


def best_next_action(rows: list[dict[str, Any]]) -> dict[str, Any]:
    partial_rows = [row for row in rows if row["new_statement_scope_status"] != "AUTO_READY"]
    if not partial_rows:
        return {
            "target_company": None,
            "recommendation": (
                "All statement-based scopes are AUTO_READY; prioritize valuation or peer-comparison scope separately."
            ),
        }
    ranked = sorted(
        partial_rows,
        key=lambda row: (
            row["new_statement_based_financials_score"],
            -len(row["missing_but_expected_facts"]),
        ),
    )
    target = ranked[0]
    return {
        "target_company": target["ticker"],
        "recommendation": target["recommended_next_action"],
        "reason": "Lowest scoped statement readiness score among requested companies.",
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


def fallback_status(report: dict[str, Any]) -> str:
    if not report:
        return "UNSUPPORTED"
    summary = report.get("summary") or {}
    if not summary.get("source_document_count"):
        return "SOURCE_BLOCKED"
    if not summary.get("canonical_facts_count"):
        return "PARSER_BLOCKED"
    return "AUTO_PARTIAL"


def save_report(report: dict[str, Any], period_from: str) -> Path:
    year = period_from[:4]
    root = get_settings().root_dir / "data" / "validation" / "auto_support_coverage"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{year}_auto_ready_blocker_analysis.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze scoped AUTO_READY blockers for real-data companies.")
    parser.add_argument("tickers", nargs="+")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    args = parser.parse_args(argv)
    tickers = args.tickers
    period_from = args.period_from
    period_to = args.period_to
    report = analyze(tickers, period_from, period_to)
    path = save_report(report, period_from)
    print(
        "\n".join(
            [
                f"companies: {', '.join(report['companies'])}",
                f"best_next_target: {report['best_next_engineering_action'].get('target_company')}",
                f"recommendation: {report['best_next_engineering_action'].get('recommendation')}",
                f"report_path: {path}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
