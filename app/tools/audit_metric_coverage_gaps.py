import argparse
import json
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.services.metrics.formula_registry import FORMULAS
from app.services.periods import periods_between

VALUATION_METRICS = {"pe_ratio", "ev_to_ebitda", "dividend_yield"}
VALUATION_INPUTS = {"market_cap", "enterprise_value", "dividend_per_share", "price"}
EBITDA_INPUTS = {"ebitda"}
NON_PARSER_INPUTS = VALUATION_INPUTS | {"previous_revenue"}
HIGH_CONFIDENCE_FACTS = {
    "current_assets",
    "current_liabilities",
    "total_assets",
    "total_equity",
    "total_debt",
    "cash_and_equivalents",
    "revenue",
    "operating_profit",
    "net_income",
}
METHODOLOGY_SENSITIVE_FACTS = {"operating_cash_flow", "capex"}


def validation_root() -> Path:
    return get_settings().root_dir / "data" / "validation"


def metrics_audit_path(ticker: str, period_from: str, period_to: str) -> Path:
    return validation_root() / ticker.upper() / f"{period_from}_{period_to}_real_metrics_audit.json"


def golden_report_path(ticker: str) -> Path:
    return validation_root() / ticker.upper() / "golden" / f"{ticker.casefold()}_2021_golden_verification_report.json"


def output_path(companies: list[str], period_from: str, period_to: str) -> Path:
    suffix = "_".join(company.upper() for company in companies)
    return validation_root() / "PEERS" / f"{suffix}_2021_metric_coverage_gaps.json"


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_metric_reports(companies: list[str], period_from: str, period_to: str) -> dict[str, dict[str, Any]]:
    return {
        company.upper(): load_json(metrics_audit_path(company, period_from, period_to))
        for company in companies
    }


def audit_gaps(
    companies: list[str],
    period_from: str,
    period_to: str,
    metric_reports: dict[str, dict[str, Any]] | None = None,
    golden_reports: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    companies = [company.upper() for company in companies]
    periods = periods_between(period_from, period_to)
    metric_reports = metric_reports or load_metric_reports(companies, period_from, period_to)
    golden_reports = golden_reports or {company: load_json(golden_report_path(company)) for company in companies}
    metric_maps = {company: metric_map(metric_reports.get(company, {})) for company in companies}
    metric_coverage = {
        company: company_coverage(metric_reports.get(company, {}), golden_reports.get(company, {}))
        for company in companies
    }
    matrix = build_missing_metric_matrix(companies, periods, metric_maps)
    priorities = build_missing_fact_priorities(companies, matrix)
    unlock_plan = build_metric_unlock_plan(priorities)
    report = {
        "companies": companies,
        "period_from": period_from,
        "period_to": period_to,
        "metric_coverage_by_company": metric_coverage,
        "missing_metric_matrix": matrix,
        "missing_fact_priority": priorities,
        "metric_unlock_plan": unlock_plan,
        "valuation_blockers": {
            "pe_ratio": ["market_cap or shares_outstanding missing"],
            "ev_to_ebitda": ["EBITDA missing", "EV missing"],
            "dividend_yield": ["dividend data missing"],
        },
        "ebitda_blockers": {
            "policy": "Do not derive EBITDA unless explicit disclosure or approved proxy policy exists"
        },
        "warnings": warnings_for_reports(metric_reports),
    }
    return report


def metric_map(report: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    rows = {}
    for row in report.get("metrics", []) or []:
        if row.get("quality_flag") == "fixture":
            continue
        rows[(row.get("period"), row.get("metric_code"))] = row
    return rows


def company_coverage(report: dict[str, Any], golden_report: dict[str, Any]) -> dict[str, Any]:
    summary = report.get("summary", {})
    return {
        "valid": summary.get("metrics_valid_count", 0),
        "questionable": summary.get("metrics_questionable_count", 0),
        "missing": summary.get("metrics_missing_count", 0),
        "verified_review_pack": golden_report.get("review_pack_status") == "PASS"
        or (report.get("metric_data_trust") or {}).get("status") == "verified_review_pack",
    }


def build_missing_metric_matrix(
    companies: list[str],
    periods: list[str],
    metric_maps: dict[str, dict[tuple[str, str], dict[str, Any]]],
) -> list[dict[str, Any]]:
    rows = []
    for period in periods:
        for metric_code in FORMULAS:
            statuses = {
                company: metric_status(metric_maps.get(company, {}).get((period, metric_code)))
                for company in companies
            }
            missing_inputs = {
                company: blocking_inputs(metric_maps.get(company, {}).get((period, metric_code)))
                for company in companies
                if statuses[company] == "missing"
            }
            if not missing_inputs and all(status == "valid" for status in statuses.values()):
                continue
            row = {
                "metric_code": metric_code,
                "period": period,
                **statuses,
                "missing_inputs_by_company": missing_inputs,
                "peer_comparison_impact": peer_impact(metric_code, statuses),
            }
            rows.append(row)
    return rows


def metric_status(row: dict[str, Any] | None) -> str:
    if not row:
        return "missing"
    if row.get("quality_flag") == "fixture":
        return "missing"
    return row.get("status") or "missing"


def blocking_inputs(row: dict[str, Any] | None) -> list[str]:
    if not row:
        return []
    inputs = row.get("blocking_inputs") or []
    if inputs:
        return sorted(set(inputs))
    values = row.get("inputs") or {}
    return sorted(name for name, value in values.items() if value is None)


def peer_impact(metric_code: str, statuses: dict[str, str]) -> str:
    if metric_code in VALUATION_METRICS:
        return "valuation_module"
    valid_count = sum(1 for status in statuses.values() if status == "valid")
    missing_count = sum(1 for status in statuses.values() if status == "missing")
    if valid_count >= 1 and missing_count >= 1:
        return "high"
    if missing_count:
        return "medium"
    return "low"


def build_missing_fact_priorities(companies: list[str], matrix: list[dict[str, Any]]) -> list[dict[str, Any]]:
    aggregate: dict[tuple[str, str], dict[str, Any]] = {}
    for row in matrix:
        metric_code = row["metric_code"]
        if metric_code in VALUATION_METRICS:
            continue
        for company, inputs in row.get("missing_inputs_by_company", {}).items():
            for fact_code in inputs:
                if not parser_actionable_fact(fact_code):
                    continue
                key = (company, fact_code)
                item = aggregate.setdefault(
                    key,
                    {
                        "fact_code": fact_code,
                        "company": company,
                        "periods": set(),
                        "unblocks_metrics": set(),
                        "unblocks_peer_comparisons": set(),
                        "reason": "",
                    },
                )
                item["periods"].add(row["period"])
                item["unblocks_metrics"].add(metric_code)
                item["unblocks_peer_comparisons"].update(peer_pairs_improved(company, companies, row))
    priorities = []
    for item in aggregate.values():
        periods = sorted(item["periods"])
        metrics = sorted(item["unblocks_metrics"])
        peer_pairs = sorted(item["unblocks_peer_comparisons"])
        priority = priority_score(item["fact_code"], periods, metrics, peer_pairs)
        priorities.append(
            {
                "fact_code": item["fact_code"],
                "company": item["company"],
                "periods": periods,
                "unblocks_metrics": metrics,
                "unblocks_peer_comparisons": peer_pairs,
                "priority_score": priority,
                "reason": priority_reason(item["fact_code"], metrics, periods),
            }
        )
    return sorted(priorities, key=lambda row: (-row["priority_score"], row["company"], row["fact_code"]))


def parser_actionable_fact(fact_code: str) -> bool:
    if fact_code in NON_PARSER_INPUTS:
        return False
    if fact_code in EBITDA_INPUTS:
        return False
    return True


def peer_pairs_improved(company: str, companies: list[str], row: dict[str, Any]) -> list[str]:
    pairs = []
    for other in companies:
        if other == company:
            continue
        if row.get(other) == "valid":
            pair = "_vs_".join(sorted([company, other]))
            pairs.append(pair)
    return pairs


def priority_score(fact_code: str, periods: list[str], metrics: list[str], peer_pairs: list[str]) -> int:
    return (
        len(metrics) * 4
        + len(periods) * 2
        + len(peer_pairs) * 3
        + fact_likelihood_score(fact_code)
    )


def fact_likelihood_score(fact_code: str) -> int:
    if fact_code in HIGH_CONFIDENCE_FACTS:
        return 5
    if fact_code in METHODOLOGY_SENSITIVE_FACTS:
        return 2
    return 1


def priority_reason(fact_code: str, metrics: list[str], periods: list[str]) -> str:
    if fact_code in {"current_assets", "current_liabilities"} and "current_ratio" in metrics:
        return f"Required for current_ratio across {len(periods)} period(s)"
    if fact_code == "operating_cash_flow":
        return "Required for fcf/fcf_margin, but availability may be statement-specific"
    if fact_code == "capex":
        return "Required for fcf/fcf_margin; methodology-sensitive and must preserve issuer capex definition"
    return f"Required input for {', '.join(metrics)} across {len(periods)} period(s)"


def build_metric_unlock_plan(priorities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for item in priorities:
        key = (item["company"], tuple(item["unblocks_metrics"]))
        plan = grouped.setdefault(
            key,
            {
                "target_company": item["company"],
                "target_facts": [],
                "expected_unlocked_metrics": list(item["unblocks_metrics"]),
                "expected_peer_comparable_metrics_added": 0,
                "priority_score": 0,
            },
        )
        plan["target_facts"].append(item["fact_code"])
        plan["expected_peer_comparable_metrics_added"] += len(item["periods"]) * max(
            1, len(item["unblocks_peer_comparisons"])
        )
        plan["priority_score"] += item["priority_score"]
    plans = sorted(grouped.values(), key=lambda row: -row["priority_score"])
    for index, plan in enumerate(plans, start=1):
        plan["rank"] = index
        plan["target_facts"] = sorted(plan["target_facts"])
        plan["recommended_next_action"] = recommended_action(plan)
    return plans


def recommended_action(plan: dict[str, Any]) -> str:
    company = plan["target_company"]
    facts = set(plan["target_facts"])
    if {"current_assets", "current_liabilities"} <= facts:
        return f"Inspect {company} balance sheet extraction for current assets/current liabilities"
    if "operating_cash_flow" in facts or "capex" in facts:
        return f"Inspect {company} cash flow / capex disclosures and preserve methodology warnings"
    return f"Inspect {company} IFRS table extraction for {', '.join(sorted(facts))}"


def warnings_for_reports(metric_reports: dict[str, dict[str, Any]]) -> list[str]:
    warnings = []
    for company, report in metric_reports.items():
        if not report:
            warnings.append(f"{company} metric audit report missing")
        if any((row.get("quality_flag") == "fixture") for row in report.get("metrics", []) or []):
            warnings.append(f"{company} fixture metrics ignored in coverage gap audit")
    return warnings


def save_report(report: dict[str, Any]) -> Path:
    path = output_path(report["companies"], report["period_from"], report["period_to"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def print_summary(report: dict[str, Any], path: Path) -> None:
    top = report["missing_fact_priority"][:5]
    top_labels = ", ".join(f"{row['company']}:{row['fact_code']}" for row in top) or "none"
    print(
        "\n".join(
            [
                f"companies: {', '.join(report['companies'])}",
                f"missing_metric_rows: {len(report['missing_metric_matrix'])}",
                f"top_priorities: {top_labels}",
                f"unlock_plan_items: {len(report['metric_unlock_plan'])}",
                f"report_path: {path}",
            ]
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit real metric coverage gaps across peer candidates.")
    parser.add_argument("companies", nargs="+")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    args = parser.parse_args(argv)
    if len(args.companies) < 2:
        raise SystemExit("At least two companies are required.")
    period_to = args.period_to
    period_from = args.period_from
    companies = args.companies
    report = audit_gaps(companies, period_from, period_to)
    path = save_report(report)
    print_summary(report, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
