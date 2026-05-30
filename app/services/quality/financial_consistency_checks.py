from dataclasses import dataclass, field
from typing import Any

from app.db.models import MetricValue, StatementFact


@dataclass
class ConsistencyCheckResult:
    passed_checks: list[str] = field(default_factory=list)
    failed_checks: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    quality_penalties: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed_checks": self.passed_checks,
            "failed_checks": self.failed_checks,
            "warnings": self.warnings,
            "quality_penalties": round(self.quality_penalties, 4),
        }


def run_financial_consistency_checks(
    facts: list[StatementFact],
    metrics: list[MetricValue] | None = None,
    tolerance: float = 0.01,
) -> ConsistencyCheckResult:
    by_period: dict[str, dict[str, float]] = {}
    duplicates: dict[tuple[str, str], list[float]] = {}
    for fact in facts:
        if fact.value is None:
            continue
        value = fact.value * (fact.unit_multiplier or 1.0)
        by_period.setdefault(fact.period, {})[fact.metric_code] = value
        duplicates.setdefault((fact.period, fact.metric_code), []).append(value)
    result = ConsistencyCheckResult()
    for period, values in by_period.items():
        _check_le(result, period, values, "current_assets", "total_assets", tolerance)
        _check_non_negative(result, period, values, "total_debt", tolerance)
        _check_non_negative(result, period, values, "cash_and_equivalents", tolerance)
        if all(key in values for key in ["total_assets", "total_equity", "total_liabilities"]):
            lhs = values["total_assets"]
            rhs = values["total_liabilities"] + values["total_equity"]
            if abs(lhs - rhs) <= max(tolerance, abs(lhs) * 0.01):
                result.passed_checks.append(f"{period}: assets approximately equal liabilities + equity")
            else:
                result.failed_checks.append(
                    {
                        "period": period,
                        "check": "assets_equal_liabilities_plus_equity",
                        "expected": rhs,
                        "actual": lhs,
                    }
                )
        for (dup_period, metric), metric_values in duplicates.items():
            if dup_period != period or len(metric_values) <= 1:
                continue
            if max(metric_values) - min(metric_values) <= max(tolerance, abs(max(metric_values)) * 0.001):
                result.passed_checks.append(f"{period}: duplicate {metric} values within tolerance")
            else:
                result.failed_checks.append(
                    {
                        "period": period,
                        "check": "same_metric_period_conflict",
                        "metric_code": metric,
                        "values": metric_values,
                    }
                )
    for metric in metrics or []:
        _check_metric_recompute(result, metric, tolerance)
    result.quality_penalties = min(1.0, len(result.failed_checks) * 0.05)
    return result


def _check_le(
    result: ConsistencyCheckResult,
    period: str,
    values: dict[str, float],
    left: str,
    right: str,
    tolerance: float,
) -> None:
    if left not in values or right not in values:
        return
    if values[left] <= values[right] + tolerance:
        result.passed_checks.append(f"{period}: {left} <= {right}")
    else:
        result.failed_checks.append(
            {
                "period": period,
                "check": f"{left}_lte_{right}",
                "left": values[left],
                "right": values[right],
            }
        )


def _check_non_negative(
    result: ConsistencyCheckResult,
    period: str,
    values: dict[str, float],
    metric: str,
    tolerance: float,
) -> None:
    if metric not in values:
        return
    if values[metric] >= -tolerance:
        result.passed_checks.append(f"{period}: {metric} >= 0")
    else:
        result.failed_checks.append({"period": period, "check": f"{metric}_non_negative", "value": values[metric]})


def _check_metric_recompute(result: ConsistencyCheckResult, metric: MetricValue, tolerance: float) -> None:
    inputs = metric.inputs_json or {}
    if metric.value is None:
        return
    if metric.metric_code == "fcf":
        ocf = inputs.get("operating_cash_flow")
        capex = inputs.get("capex")
        if isinstance(ocf, int | float) and isinstance(capex, int | float):
            _compare_metric(result, metric, ocf - capex, tolerance)
    if metric.metric_code == "debt_to_equity":
        debt = inputs.get("total_debt")
        equity = inputs.get("total_equity")
        if isinstance(debt, int | float) and isinstance(equity, int | float) and equity != 0:
            _compare_metric(result, metric, debt / equity, tolerance)


def _compare_metric(result: ConsistencyCheckResult, metric: MetricValue, expected: float, tolerance: float) -> None:
    if abs((metric.value or 0.0) - expected) <= max(tolerance, abs(expected) * 0.0001):
        result.passed_checks.append(f"{metric.period}: {metric.metric_code} recomputes correctly")
    else:
        result.failed_checks.append(
            {
                "period": metric.period,
                "check": f"{metric.metric_code}_recompute",
                "expected": expected,
                "actual": metric.value,
            }
        )
