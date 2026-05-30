from typing import Any

from app.db.models import StatementFact

VALUATION_METRICS = {"pe_ratio", "ev_to_ebitda", "dividend_yield"}
MARKET_INPUT_METRICS = {"pe_ratio", "ev_to_ebitda", "dividend_yield"}
EBITDA_METRICS = {"ebitda_margin", "net_debt_to_ebitda", "ev_to_ebitda"}
MARGIN_METRICS = {
    "ebitda_margin": ["ebitda", "revenue"],
    "operating_margin": ["operating_profit", "revenue"],
    "net_margin": ["net_income", "revenue"],
}


def audit_metric_rows(metrics: list[dict[str, Any]], facts: list[StatementFact]) -> list[dict[str, Any]]:
    fact_map = {(fact.period, fact.metric_code): fact for fact in facts}
    rows = []
    for metric in metrics:
        period = metric.get("period")
        code = metric.get("metric_code")
        input_names = [name for name in (metric.get("inputs") or {}) if name in _fact_input_names(code)]
        input_facts = {name: fact_map.get((period, name)) for name in input_names}
        input_period_types = {
            name: fact.period_type for name, fact in input_facts.items() if fact is not None
        }
        input_source_roles = {
            name: (fact.source_location or {}).get("source_role")
            for name, fact in input_facts.items()
            if fact is not None
        }
        input_quality_flags = {
            name: fact.quality_flag for name, fact in input_facts.items() if fact is not None
        }
        input_source_locations = [
            fact.source_location for fact in input_facts.values() if fact is not None and fact.source_location
        ]
        status, warnings = classify_metric(metric, input_facts, input_period_types)
        missing_reason, blocking_inputs, fix_recommendation = missing_details(metric, input_facts, input_period_types)
        questionable_reason = questionable_details(metric, input_facts, input_period_types, status, warnings)
        rows.append(
            {
                "metric_code": code,
                "period": period,
                "status": status,
                "missing_reason": missing_reason if status == "missing" else None,
                "questionable_reason": questionable_reason if status == "questionable" else None,
                "blocking_inputs": blocking_inputs if status == "missing" else [],
                "fix_recommendation": fix_recommendation if status in {"missing", "questionable"} else None,
                "value": metric.get("value"),
                "quality_flag": metric.get("quality_flag"),
                "formula": metric.get("formula"),
                "inputs": metric.get("inputs") or {},
                "input_period_types": input_period_types,
                "input_source_roles": input_source_roles,
                "input_quality_flags": input_quality_flags,
                "methodology_warnings": warnings,
                "input_source_locations": input_source_locations,
                "source_locations": input_source_locations,
            }
        )
    return rows


def classify_metric(
    metric: dict[str, Any],
    input_facts: dict[str, StatementFact | None],
    input_period_types: dict[str, str],
) -> tuple[str, list[str]]:
    code = metric.get("metric_code")
    value = metric.get("value")
    warnings = list(metric.get("warnings") or [])
    if code in VALUATION_METRICS:
        return ("missing" if value is None else "invalid"), warnings or ["valuation input unavailable"]
    if value is None or metric.get("quality_flag") == "missing":
        return "missing", warnings
    if any((fact and fact.quality_flag == "conflicting_sources") for fact in input_facts.values()):
        return "invalid", warnings + ["Metric uses conflicting input fact"]
    if any((fact and fact.quality_flag == "low_confidence_parse") for fact in input_facts.values()):
        return "questionable", warnings + ["Metric uses low-confidence input fact"]
    if code in MARGIN_METRICS:
        types = {input_period_types.get(name) for name in MARGIN_METRICS[code] if input_period_types.get(name)}
        if len(types) == 1:
            return "valid", warnings
        return "invalid", warnings + ["Margin inputs have mixed or missing period coverage"]
    if code == "revenue_growth":
        revenue_type = input_period_types.get("revenue")
        if revenue_type in {"ytd", "annual"}:
            return "questionable", warnings + ["Revenue growth uses cumulative YTD/annual disclosure, not standalone quarter"]
    if code in {"roe", "roa"}:
        return (
            "questionable",
            warnings + ["Return metric uses income with balance sheet snapshots; review annualization methodology"],
        )
    if code == "fcf":
        types = {input_period_types.get(name) for name in ["operating_cash_flow", "capex"] if input_period_types.get(name)}
        return ("valid", warnings) if len(types) == 1 else ("invalid", warnings + ["FCF inputs have mixed period coverage"])
    if code == "fcf_margin":
        types = {
            input_period_types.get(name)
            for name in ["operating_cash_flow", "capex", "revenue"]
            if input_period_types.get(name)
        }
        return (
            ("valid", warnings)
            if len(types) == 1
            else ("invalid", warnings + ["FCF margin inputs have mixed period coverage"])
        )
    if code in {"current_ratio", "debt_to_equity"}:
        if all(item == "balance_sheet_snapshot" for item in input_period_types.values()):
            return "valid", warnings
        return "invalid", warnings + ["Metric requires balance_sheet_snapshot inputs"]
    if code == "net_debt_to_ebitda":
        return "missing", warnings + ["EBITDA unavailable or incompatible"]
    return "valid", warnings


def missing_details(
    metric: dict[str, Any],
    input_facts: dict[str, StatementFact | None],
    input_period_types: dict[str, str],
) -> tuple[str | None, list[str], str | None]:
    code = metric.get("metric_code")
    inputs = metric.get("inputs") or {}
    if metric.get("value") is not None and metric.get("quality_flag") != "missing":
        return None, [], None
    if code in {"ebitda_margin", "net_debt_to_ebitda"}:
        return "missing_ebitda", ["ebitda"], "Leave missing until EBITDA is explicitly disclosed in source documents."
    if code == "ev_to_ebitda":
        if inputs.get("ebitda") is None and inputs.get("ebitda_ttm") is None:
            return "missing_ebitda", ["ebitda", "ev"], "Requires explicit EBITDA disclosure and EV/market inputs."
        return "missing_ev", ["ev"], "Requires enterprise value input from market data."
    if code == "pe_ratio":
        return "missing_market_cap", ["market_cap"], "Requires market capitalization or price and shares outstanding."
    if code == "dividend_yield":
        return "missing_dividend", ["dividends", "price"], "Requires dividend and price inputs."
    if code in {"roe", "roa"} and "TTM unavailable" in " ".join(metric.get("warnings") or []):
        return (
            "incompatible_period_semantics",
            ["net_income_ttm"],
            "Do not calculate return metrics until compatible TTM income coverage is available.",
        )
    if code == "revenue_growth" and inputs.get("previous_revenue") is None:
        return (
            "incompatible_period_semantics",
            ["previous_revenue"],
            "Requires prior comparable revenue period; first period in the range remains missing.",
        )
    required = _fact_input_names(code)
    missing_inputs = sorted(name for name in required if name in inputs and inputs.get(name) is None)
    if missing_inputs:
        return "missing_input_fact", missing_inputs, "Add parser coverage only if explicit source rows exist."
    if input_period_types and len(set(input_period_types.values())) > 1:
        return "incompatible_period_semantics", [], "Align period semantics before calculating this metric."
    return "not_applicable_to_current_data", [], "No methodologically acceptable calculation is available from current data."


def questionable_details(
    metric: dict[str, Any],
    input_facts: dict[str, StatementFact | None],
    input_period_types: dict[str, str],
    status: str,
    warnings: list[str],
) -> str | None:
    if status != "questionable":
        return None
    code = metric.get("metric_code")
    if any(fact and fact.quality_flag == "derived" for fact in input_facts.values()):
        return "derived_input_used"
    if any(fact and fact.quality_flag not in {"exact", "derived"} for fact in input_facts.values()):
        return "unverified_fact_input"
    if code in {"roe", "roa"}:
        if any("Average" in warning and "unavailable" in warning for warning in warnings):
            return "average_balance_sheet_unavailable"
        return "ytd_income_with_snapshot_balance_sheet"
    if code == "revenue_growth" and input_period_types.get("revenue") in {"ytd", "annual"}:
        return "incomplete_period_coverage"
    if warnings:
        return "methodology_warning"
    return "methodology_warning"


def metric_status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "metrics_valid_count": sum(1 for row in rows if row["status"] == "valid"),
        "metrics_questionable_count": sum(1 for row in rows if row["status"] == "questionable"),
        "metrics_invalid_count": sum(1 for row in rows if row["status"] == "invalid"),
        "metrics_missing_count": sum(1 for row in rows if row["status"] == "missing"),
    }


def _fact_input_names(code: str | None) -> set[str]:
    base = {
        "revenue",
        "ebitda",
        "operating_profit",
        "net_income",
        "total_assets",
        "total_equity",
        "total_debt",
        "cash_and_equivalents",
        "current_assets",
        "current_liabilities",
        "operating_cash_flow",
        "capex",
    }
    by_metric = {
        "revenue_growth": {"revenue"},
        "ebitda_margin": {"ebitda", "revenue"},
        "operating_margin": {"operating_profit", "revenue"},
        "net_margin": {"net_income", "revenue"},
        "roe": {"net_income", "total_equity"},
        "roa": {"net_income", "total_assets"},
        "debt_to_equity": {"total_debt", "total_equity"},
        "net_debt_to_ebitda": {"total_debt", "cash_and_equivalents", "ebitda"},
        "current_ratio": {"current_assets", "current_liabilities"},
        "fcf": {"operating_cash_flow", "capex"},
        "fcf_margin": {"operating_cash_flow", "capex", "revenue"},
    }
    return by_metric.get(code or "", base)


def questionable_metrics_breakdown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        if row["status"] != "questionable":
            continue
        reason = row.get("questionable_reason") or "methodology_warning"
        fix_type = "cannot_fix"
        can_fix = False
        recommendation = "Keep metric as questionable until methodology is approved."
        if reason in {"ytd_income_with_snapshot_balance_sheet", "average_balance_sheet_unavailable"}:
            fix_type = "requires_manual_policy"
            recommendation = "Requires explicit policy for annualization and average balance sheet snapshots."
        elif reason == "incomplete_period_coverage":
            fix_type = "period_semantics"
            recommendation = "Use only comparable standalone-quarter or comparable cumulative-period growth."
        elif reason == "derived_input_used":
            fix_type = "requires_manual_policy"
            recommendation = "Metric is usable with warning if derived input methodology is accepted."
        out.append(
            {
                "metric_code": row["metric_code"],
                "period": row["period"],
                "reason": reason,
                "can_be_fixed_now": can_fix,
                "fix_type": fix_type,
                "recommendation": recommendation,
            }
        )
    return out


def missing_metrics_breakdown(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {
        "fixable_by_parser": [],
        "unavailable_in_source": [],
        "requires_market_inputs": [],
        "requires_explicit_disclosure": [],
        "blocked_by_period_semantics": [],
    }
    for row in rows:
        if row["status"] != "missing":
            continue
        item = {
            "metric_code": row["metric_code"],
            "period": row["period"],
            "missing_reason": row.get("missing_reason"),
            "blocking_inputs": row.get("blocking_inputs", []),
            "fix_recommendation": row.get("fix_recommendation"),
        }
        reason = row.get("missing_reason")
        market_reasons = {"missing_market_cap", "missing_ev", "missing_price", "missing_dividend"}
        if row["metric_code"] in MARKET_INPUT_METRICS or reason in market_reasons:
            groups["requires_market_inputs"].append(item)
        elif reason == "missing_ebitda":
            groups["requires_explicit_disclosure"].append(item)
        elif reason == "missing_input_fact":
            groups["fixable_by_parser"].append(item)
        elif reason == "incompatible_period_semantics":
            groups["blocked_by_period_semantics"].append(item)
        else:
            groups["unavailable_in_source"].append(item)
    return groups


def metric_decision_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    codes = sorted({row["metric_code"] for row in rows})
    table = []
    for code in codes:
        subset = [row for row in rows if row["metric_code"] == code]
        valid = sum(1 for row in subset if row["status"] == "valid")
        questionable = sum(1 for row in subset if row["status"] == "questionable")
        missing = sum(1 for row in subset if row["status"] == "missing")
        invalid = sum(1 for row in subset if row["status"] == "invalid")
        decision, notes = metric_decision(code, valid, questionable, missing, invalid, subset)
        table.append(
            {
                "metric_code": code,
                "periods_valid": valid,
                "periods_questionable": questionable,
                "periods_missing": missing,
                "periods_invalid": invalid,
                "decision": decision,
                "notes": notes,
            }
        )
    return table


def metric_decision(
    code: str,
    valid: int,
    questionable: int,
    missing: int,
    invalid: int,
    rows: list[dict[str, Any]],
) -> tuple[str, str]:
    if code in {"pe_ratio", "ev_to_ebitda", "dividend_yield"}:
        return "requires_market_data", "Missing market valuation/dividend inputs."
    if code in {"ebitda_margin", "net_debt_to_ebitda"}:
        return "requires_explicit_disclosure", "EBITDA is not explicitly available in parsed financial statements."
    if code in {"roe", "roa"} and questionable:
        return "needs_methodology_decision", "Return metrics depend on TTM/annualization and average balance sheet policy."
    if code == "revenue_growth" and questionable:
        return "usable_with_warning", "Growth is based on cumulative YTD/annual disclosures, not standalone quarters."
    if invalid:
        return "needs_methodology_decision", "At least one period is invalid under current period semantics."
    if valid and not missing and not questionable:
        if any("derived" in (row.get("input_quality_flags") or {}).values() for row in rows):
            return "usable_with_warning", "Calculated with at least one derived input; methodology is disclosed."
        return "usable", "Calculated from compatible real financial statement inputs."
    if valid and (missing or questionable):
        return "usable_with_warning", "Available for some periods only or requires methodology caveats."
    if missing:
        return "not_available", "Not available from current real data and methodology."
    return "not_available", "No usable calculation under current data."
