import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.services.metrics.metric_methodology_registry import METHODOLOGIES
from app.services.periods import periods_between
from app.tools.audit_real_metrics import run_audit
from app.tools.verify_golden_facts import verify as verify_golden_facts


def output_path() -> Path:
    return get_settings().root_dir / "data" / "validation" / "PEERS" / "2021_statement_based_metric_audit.json"


def audit_statement_metrics(companies: list[str], period_from: str, period_to: str) -> dict[str, Any]:
    companies = [company.upper() for company in companies]
    rows = []
    for company in companies:
        metric_report = run_audit(company, period_from, period_to)
        golden = verify_golden_facts(company, period_from, period_to)
        trust = metric_report.get("metric_data_trust") or metric_data_trust_from_golden(golden)
        metric_by_key = {
            (row.get("period"), row.get("metric_code")): row
            for row in metric_report.get("metrics", [])
        }
        for period in periods_between(period_from, period_to):
            for metric_code, methodology in METHODOLOGIES.items():
                metric = metric_by_key.get((period, metric_code), {})
                rows.append(audit_row(company, period, metric_code, metric, methodology, golden, trust))
    report = {
        "companies": companies,
        "period_from": period_from,
        "period_to": period_to,
        "rows": rows,
        "summary_by_company": summary_by_company(rows),
        "top_missing_inputs": top_missing_inputs(rows),
        "valuation_metrics_blocked": valuation_metrics_blocked(rows),
        "capex_warnings_preserved": capex_warnings_preserved(rows),
        "warnings": [
            (
                "Metrics are audited as statement-based calculations from canonical facts; "
                "valuation metrics remain blocked without market inputs."
            )
        ],
    }
    path = output_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return report


def metric_data_trust_from_golden(golden: dict[str, Any]) -> dict[str, Any]:
    if golden.get("review_pack_status") == "PASS":
        return {"status": "verified_review_pack", "review_pack_accuracy": golden.get("review_pack_accuracy")}
    if golden.get("review_pack_verified"):
        return {"status": "partially_verified", "review_pack_accuracy": golden.get("review_pack_accuracy")}
    return {"status": "unverified", "review_pack_accuracy": None}


def audit_row(
    company: str,
    period: str,
    metric_code: str,
    metric: dict[str, Any],
    methodology,
    golden: dict[str, Any],
    trust: dict[str, Any],
) -> dict[str, Any]:
    status = metric.get("status") or "missing"
    input_locations = metric.get("input_source_locations") or metric.get("source_locations") or []
    available_inputs = sorted((metric.get("input_period_types") or {}).keys())
    missing_inputs = metric.get("blocking_inputs") or [
        name
        for name, value in (metric.get("inputs") or {}).items()
        if name in methodology.required_facts and value is None
    ]
    warnings = list(metric.get("methodology_warnings") or [])
    if methodology.market_inputs_required and status != "missing":
        warnings.append("valuation metric calculated despite market_inputs_required; review guardrails")
    return {
        "metric_code": metric_code,
        "company": company,
        "period": period,
        "status": "calculated" if status == "valid" else status,
        "formula": methodology.formula,
        "required_inputs": list(methodology.required_facts),
        "available_inputs": available_inputs,
        "missing_inputs": sorted(set(missing_inputs)),
        "source_statement_types": source_statement_types(input_locations),
        "input_traceability_complete": traceability_complete(input_locations, available_inputs),
        "manual_verification_status": manual_status(golden),
        "metric_data_trust": trust.get("status", "unverified"),
        "market_inputs_required": methodology.market_inputs_required,
        "required_statement_sources": methodology.required_statement_sources,
        "period_requirements": methodology.period_requirements,
        "denominator_rules": methodology.denominator_rules,
        "warnings": warnings,
    }


def source_statement_types(locations: list[dict[str, Any]]) -> list[str]:
    values = []
    for location in locations:
        statement = location.get("statement_context") or location.get("table_title") or location.get("table")
        if statement:
            values.append(statement)
    return sorted(set(values))


def traceability_complete(locations: list[dict[str, Any]], available_inputs: list[str]) -> bool:
    if not available_inputs:
        return False
    if len(locations) < len(available_inputs):
        return False
    return all(location.get("source_url") and location.get("raw_label") for location in locations)


def manual_status(golden: dict[str, Any]) -> str:
    if golden.get("review_pack_status") == "PASS":
        return "verified_review_pack"
    if golden.get("review_pack_verified"):
        return "partially_verified"
    return "unverified"


def summary_by_company(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        summary = out.setdefault(row["company"], {"calculated": 0, "missing": 0, "questionable": 0})
        if row["status"] in summary:
            summary[row["status"]] += 1
    return out


def top_missing_inputs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter: Counter[tuple[str, str]] = Counter()
    for row in rows:
        for item in row.get("missing_inputs", []):
            counter[(row["company"], item)] += 1
    return [
        {"company": company, "input": input_name, "count": count}
        for (company, input_name), count in counter.most_common(15)
    ]


def valuation_metrics_blocked(rows: list[dict[str, Any]]) -> bool:
    valuation = [row for row in rows if row["market_inputs_required"]]
    return bool(valuation) and all(row["status"] == "missing" for row in valuation)


def capex_warnings_preserved(rows: list[dict[str, Any]]) -> bool:
    capex_rows = [row for row in rows if row["metric_code"] in {"fcf", "fcf_margin"} and row["company"] == "GAZP"]
    if not capex_rows:
        return True
    return any(
        any("reported segment capital expenditures" in warning for warning in row.get("warnings", []))
        for row in capex_rows
    )


def print_summary(report: dict[str, Any]) -> None:
    print(
        "\n".join(
            [
                f"companies: {', '.join(report['companies'])}",
                f"report_path: {output_path()}",
                f"valuation_metrics_blocked: {report['valuation_metrics_blocked']}",
                f"capex_warnings_preserved: {report['capex_warnings_preserved']}",
            ]
        )
    )
    for company, summary in report["summary_by_company"].items():
        print(
            f"{company}: calculated={summary['calculated']} "
            f"questionable={summary['questionable']} missing={summary['missing']}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit statement-based metric construction.")
    parser.add_argument("companies", nargs="+")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    args = parser.parse_args(argv)
    if len(args.companies) < 1:
        raise SystemExit("At least one company is required.")
    period_from = args.period_from
    period_to = args.period_to
    companies = args.companies
    report = audit_statement_metrics(companies, period_from, period_to)
    print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
