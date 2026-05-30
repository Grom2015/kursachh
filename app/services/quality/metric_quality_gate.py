from dataclasses import dataclass, field
from typing import Any

from app.services.metrics.metric_methodology_registry import METHODOLOGIES

VALUATION_METRICS = {
    code
    for code, methodology in METHODOLOGIES.items()
    if methodology.support_class == "market_valuation_required"
}
EBITDA_METRICS = {
    code
    for code, methodology in METHODOLOGIES.items()
    if methodology.support_class in {"explicit_disclosure_required", "market_valuation_required"}
    and "ebitda" in methodology.required_facts
}
CORE_STATEMENT_METRICS = {
    code
    for code, methodology in METHODOLOGIES.items()
    if methodology.support_class == "core_statement_based"
}
METHODOLOGY_SENSITIVE_METRICS = {
    code
    for code, methodology in METHODOLOGIES.items()
    if methodology.support_class == "methodology_sensitive"
}
EXPLICIT_DISCLOSURE_REQUIRED_METRICS = {
    code
    for code, methodology in METHODOLOGIES.items()
    if methodology.support_class == "explicit_disclosure_required"
}


@dataclass
class MetricQualityResult:
    status: str
    score: float
    valid_count: int
    questionable_count: int
    invalid_count: int
    missing_count: int
    eligible_metrics_count: int = 0
    calculated_eligible_metrics_count: int = 0
    questionable_eligible_metrics_count: int = 0
    missing_expected_metrics_count: int = 0
    unsupported_metrics_count: int = 0
    unsupported_by_policy: list[dict[str, Any]] = field(default_factory=list)
    missing_but_expected: list[dict[str, Any]] = field(default_factory=list)
    methodology_sensitive: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "score": self.score,
            "valid_count": self.valid_count,
            "questionable_count": self.questionable_count,
            "invalid_count": self.invalid_count,
            "missing_count": self.missing_count,
            "eligible_metrics_count": self.eligible_metrics_count,
            "calculated_eligible_metrics_count": self.calculated_eligible_metrics_count,
            "questionable_eligible_metrics_count": self.questionable_eligible_metrics_count,
            "missing_expected_metrics_count": self.missing_expected_metrics_count,
            "unsupported_metrics_count": self.unsupported_metrics_count,
            "unsupported_by_policy": self.unsupported_by_policy,
            "missing_but_expected": self.missing_but_expected,
            "methodology_sensitive": self.methodology_sensitive,
            "blockers": self.blockers,
            "warnings": self.warnings,
        }


def evaluate_metric_quality(metrics: list[Any], data_mode: str = "real") -> MetricQualityResult:
    blockers: list[str] = []
    warnings: list[str] = []
    rows = [_metric_row(metric) for metric in metrics]
    valid = [row for row in rows if _status(row) in {"valid", "calculated", "derived"} and row.get("quality_flag") != "missing"]
    questionable = [row for row in rows if _status(row) == "questionable" or row.get("quality_flag") == "derived"]
    invalid = [row for row in rows if _status(row) == "invalid"]
    missing = [row for row in rows if _status(row) == "missing" or row.get("quality_flag") == "missing"]

    if data_mode == "real":
        fixture_rows = [
            row
            for row in rows
            if row.get("quality_flag") == "fixture"
            or (row.get("inputs") or {}).get("source_type") == "fixture"
            or "fixture" in " ".join(row.get("warnings") or []).casefold()
        ]
        if fixture_rows:
            blockers.append("Fixture metrics are present in real mode.")
    for row in rows:
        code = row.get("metric_code")
        warnings_text = " ".join(row.get("warnings") or []).casefold()
        if "denominator_zero" in warnings_text and row.get("value") is not None:
            blockers.append("Metric with denominator_zero warning has a non-null value.")
        if code in VALUATION_METRICS and row.get("value") is not None:
            blockers.append("Valuation metric calculated without validated market inputs.")
        if code in EBITDA_METRICS and "ebitda" in warnings_text and row.get("value") is not None:
            blockers.append("EBITDA-based metric calculated despite missing EBITDA disclosure.")
        if code == "fcf" and row.get("value") is not None:
            inputs = row.get("inputs") or {}
            ocf = inputs.get("operating_cash_flow")
            capex = inputs.get("capex")
            if isinstance(ocf, int | float) and isinstance(capex, int | float) and abs(row["value"] - (ocf - capex)) > 0.01:
                blockers.append("FCF value does not recompute from operating cash flow minus capex.")
        if "ytd" in warnings_text and "standalone quarter" in warnings_text:
            blockers.append("YTD data is treated as standalone quarter.")
        if code in {"fcf", "fcf_margin"} and "segment capital expenditures" in warnings_text:
            warnings.append("Capex methodology warning preserved for capex-based metric.")

    unsupported_by_policy = [_unsupported_row(row) for row in rows if _is_unsupported_by_policy(row)]
    eligible_rows = [
        row
        for row in rows
        if row.get("metric_code") in CORE_STATEMENT_METRICS
    ]
    calculated_eligible = [
        row
        for row in eligible_rows
        if _status(row) in {"valid", "calculated", "derived"} and row.get("quality_flag") != "missing"
    ]
    questionable_eligible = [
        row
        for row in eligible_rows
        if _status(row) == "questionable" or row.get("quality_flag") == "derived"
    ]
    missing_expected = [
        _missing_expected_row(row)
        for row in eligible_rows
        if _status(row) == "missing" or row.get("quality_flag") == "missing"
    ]
    methodology_sensitive = [
        _methodology_sensitive_row(row)
        for row in rows
        if row.get("metric_code") in METHODOLOGY_SENSITIVE_METRICS
    ]
    denominator = len(eligible_rows)
    coverage = len(calculated_eligible) / denominator if denominator else 0.0
    score = coverage
    if invalid:
        score -= 0.2
    if blockers:
        score -= 0.4
    score = max(0.0, min(score, 1.0))
    status = "pass"
    if blockers or invalid:
        status = "fail"
    elif not calculated_eligible:
        status = "missing"
    elif missing_expected or questionable_eligible:
        status = "partial"
    return MetricQualityResult(
        status=status,
        score=round(score, 4),
        valid_count=len(valid),
        questionable_count=len(questionable),
        invalid_count=len(invalid),
        missing_count=len(missing),
        eligible_metrics_count=len(eligible_rows),
        calculated_eligible_metrics_count=len(calculated_eligible),
        questionable_eligible_metrics_count=len(questionable_eligible),
        missing_expected_metrics_count=len(missing_expected),
        unsupported_metrics_count=len(unsupported_by_policy),
        unsupported_by_policy=unsupported_by_policy,
        missing_but_expected=missing_expected,
        methodology_sensitive=methodology_sensitive,
        blockers=sorted(set(blockers)),
        warnings=sorted(set(warnings)),
    )


def _metric_row(metric: Any) -> dict[str, Any]:
    if isinstance(metric, dict):
        return metric
    return {
        "metric_code": getattr(metric, "metric_code", None),
        "value": getattr(metric, "value", None),
        "quality_flag": getattr(metric, "quality_flag", None),
        "warnings": getattr(metric, "warnings_json", None) or [],
        "inputs": getattr(metric, "inputs_json", None) or {},
        "status": getattr(metric, "status", None),
    }


def _status(row: dict[str, Any]) -> str:
    if row.get("status"):
        return str(row["status"])
    if row.get("quality_flag") == "missing" or row.get("value") is None:
        return "missing"
    if row.get("quality_flag") == "derived":
        return "questionable"
    return "valid"


def _is_unsupported_by_policy(row: dict[str, Any]) -> bool:
    code = row.get("metric_code")
    if code in VALUATION_METRICS:
        return True
    if code in EXPLICIT_DISCLOSURE_REQUIRED_METRICS and (_status(row) == "missing" or row.get("quality_flag") == "missing"):
        return True
    return False


def _unsupported_row(row: dict[str, Any]) -> dict[str, Any]:
    code = row.get("metric_code")
    methodology = METHODOLOGIES.get(code)
    reason = "unsupported_by_policy"
    if methodology and methodology.support_class == "market_valuation_required":
        reason = "market_or_valuation_inputs_required"
    elif methodology and methodology.support_class == "explicit_disclosure_required":
        reason = "explicit_disclosure_required"
    return {
        "metric_code": code,
        "period": row.get("period"),
        "support_class": methodology.support_class if methodology else "unknown",
        "reason": reason,
    }


def _missing_expected_row(row: dict[str, Any]) -> dict[str, Any]:
    code = row.get("metric_code")
    methodology = METHODOLOGIES.get(code)
    return {
        "metric_code": code,
        "period": row.get("period"),
        "support_class": methodology.support_class if methodology else "unknown",
        "required_facts": list(methodology.required_facts) if methodology else [],
        "reason": "missing_expected_statement_inputs",
    }


def _methodology_sensitive_row(row: dict[str, Any]) -> dict[str, Any]:
    code = row.get("metric_code")
    methodology = METHODOLOGIES.get(code)
    return {
        "metric_code": code,
        "period": row.get("period"),
        "status": _status(row),
        "quality_flag": row.get("quality_flag"),
        "support_class": methodology.support_class if methodology else "unknown",
        "warnings": row.get("warnings") or [],
    }
