from dataclasses import dataclass, field
from typing import Any

from app.services.quality.metric_quality_gate import MetricQualityResult

STATEMENT_SCOPE = "statement_based_financials"
MARKET_SCOPE = "market_technical_analysis"
VALUATION_SCOPE = "valuation_metrics"
PEER_SCOPE = "peer_comparison"
LLM_SCOPE = "llm_summary"


@dataclass
class ReadinessScopeResult:
    scope_statuses: dict[str, dict[str, Any]]
    supported_output_scopes: list[str] = field(default_factory=list)
    unsupported_output_scopes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_statuses": self.scope_statuses,
            "supported_output_scopes": self.supported_output_scopes,
            "unsupported_output_scopes": self.unsupported_output_scopes,
        }


def evaluate_readiness_scopes(
    parser_status: str,
    document_status: str,
    metric_result: MetricQualityResult,
    consistency_summary: dict[str, Any],
    manual_verification: dict[str, Any] | None = None,
    source_package_status: str = "READY",
    market_data_available: bool = True,
    peer_comparison_available: bool = False,
    statement_ready_threshold: float = 0.75,
) -> ReadinessScopeResult:
    manual = manual_verification or {}
    manual_failed = manual.get("status") == "fail" or manual.get("review_pack_status") == "FAIL"
    critical_blockers = []
    if document_status == "blocked" or source_package_status in {"NOT_READY", "SOURCE_BLOCKED"}:
        critical_blockers.append("source_or_document_blocked")
    if parser_status in {"parser_blocked", "fail"}:
        critical_blockers.append("parser_blocked")
    if consistency_summary.get("failed_checks"):
        critical_blockers.append("consistency_checks_failed")
    if manual_failed:
        critical_blockers.append("manual_qa_failed")

    statement_status = "AUTO_PARTIAL"
    if critical_blockers:
        statement_status = "LOW_CONFIDENCE" if metric_result.calculated_eligible_metrics_count else "UNSUPPORTED"
    elif (
        metric_result.score >= statement_ready_threshold
        and metric_result.calculated_eligible_metrics_count > 0
        and not metric_result.blockers
    ):
        statement_status = "AUTO_READY"
    elif metric_result.calculated_eligible_metrics_count == 0:
        statement_status = "UNSUPPORTED"

    scopes = {
        STATEMENT_SCOPE: {
            "status": statement_status,
            "score": metric_result.score,
            "eligible_metrics_count": metric_result.eligible_metrics_count,
            "calculated_eligible_metrics_count": metric_result.calculated_eligible_metrics_count,
            "questionable_eligible_metrics_count": metric_result.questionable_eligible_metrics_count,
            "missing_expected_metrics_count": metric_result.missing_expected_metrics_count,
            "unsupported_metrics_count": metric_result.unsupported_metrics_count,
            "unsupported_by_policy": metric_result.unsupported_by_policy,
            "missing_but_expected": metric_result.missing_but_expected,
            "methodology_sensitive": metric_result.methodology_sensitive,
            "critical_blockers": critical_blockers,
        },
        MARKET_SCOPE: {
            "status": "AUTO_READY" if market_data_available else "UNSUPPORTED",
            "score": 1.0 if market_data_available else 0.0,
        },
        VALUATION_SCOPE: {
            "status": "UNAVAILABLE",
            "reason": "market_cap / EV / dividends missing",
            "unsupported_metrics": [
                row
                for row in metric_result.unsupported_by_policy
                if row.get("reason") == "market_or_valuation_inputs_required"
            ],
        },
        PEER_SCOPE: {
            "status": "AUTO_PARTIAL" if peer_comparison_available else "UNSUPPORTED",
            "reason": None if peer_comparison_available else "compatible peer metrics insufficient or not requested",
        },
        LLM_SCOPE: {
            "status": "AUTO_READY" if statement_status in {"AUTO_READY", "AUTO_PARTIAL"} else "AUTO_PARTIAL",
            "reason": "LLM summary must state scope limitations unless all requested scopes are AUTO_READY.",
        },
    }
    supported = [
        scope
        for scope, payload in scopes.items()
        if payload.get("status") in {"AUTO_READY", "AUTO_PARTIAL"}
    ]
    unsupported = [scope for scope in scopes if scope not in supported]
    return ReadinessScopeResult(scopes, supported, unsupported)
