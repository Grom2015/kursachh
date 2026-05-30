from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FactInput:
    metric_code: str
    value: float
    source_location: dict[str, Any] | None = None


@dataclass(frozen=True)
class DerivedFactResult:
    metric_code: str
    value: float
    formula: str
    inputs_json: dict[str, Any]
    warning: str
    quality_flag: str = "derived"


def derive_total_debt(components: dict[str, FactInput]) -> DerivedFactResult | None:
    explicit = components.get("total_debt")
    if explicit:
        return DerivedFactResult(
            "total_debt",
            explicit.value,
            "explicit_total_debt",
            {"total_debt": source_payload(explicit)},
            "total_debt explicitly disclosed by issuer.",
            "exact",
        )
    included = components.get("short_term_debt_including_current_portion")
    net_long = components.get("long_term_debt_net_of_current_portion")
    if included and net_long:
        return debt_result(
            included,
            net_long,
            "short_term_debt_including_current_portion + long_term_debt_net_of_current_portion",
            "total_debt derived from debt note components; methodology avoids double-counting current portion of long-term debt.",
        )
    short = components.get("total_short_term_debt_excluding_current_portion") or components.get("total_short_term_debt")
    gross_long = components.get("gross_long_term_debt") or components.get("total_long_term_debt_gross")
    if short and gross_long:
        return debt_result(
            short,
            gross_long,
            "total_short_term_debt_excluding_current_portion + gross_long_term_debt",
            "total_debt derived from short-term debt excluding current portion plus gross long-term debt.",
        )
    forbidden_left = components.get("short_term_debt_including_current_portion")
    forbidden_right = components.get("gross_long_term_debt") or components.get("total_long_term_debt_gross")
    if forbidden_left and forbidden_right:
        return None
    return None


def debt_result(left: FactInput, right: FactInput, formula: str, warning: str) -> DerivedFactResult:
    return DerivedFactResult(
        "total_debt",
        left.value + right.value,
        formula,
        {
            left.metric_code: source_payload(left),
            right.metric_code: source_payload(right),
        },
        warning,
    )


def source_payload(item: FactInput) -> dict[str, Any]:
    return {"value": item.value, "source_location": item.source_location}
