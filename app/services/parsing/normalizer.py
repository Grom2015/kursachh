KNOWN_METRICS = {
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
    "dividends_paid",
}

QUALITY_FLAGS = {
    "exact",
    "derived",
    "missing",
    "conflicting_sources",
    "low_confidence_parse",
    "fixture",
    "unavailable",
    "manual_review_required",
}


def normalize_metric_code(value: str) -> str:
    code = (value or "").strip().casefold().replace(" ", "_")
    return code


def normalize_quality_flag(value: str | None) -> str:
    return value if value in QUALITY_FLAGS else "manual_review_required"

