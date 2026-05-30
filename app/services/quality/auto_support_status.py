from dataclasses import dataclass, field
from typing import Any

from app.db.models import MetricValue, ReportDocument, StatementFact
from app.services.periods import periods_between
from app.services.quality.financial_consistency_checks import run_financial_consistency_checks
from app.services.quality.metric_quality_gate import evaluate_metric_quality
from app.services.quality.parser_quality_gate import evaluate_parser_quality
from app.services.quality.readiness_scope import evaluate_readiness_scopes
from app.services.reports.document_validator import DocumentValidator

AUTO_READY = "AUTO_READY"
AUTO_PARTIAL = "AUTO_PARTIAL"
SOURCE_BLOCKED = "SOURCE_BLOCKED"
PARSER_BLOCKED = "PARSER_BLOCKED"
LOW_CONFIDENCE = "LOW_CONFIDENCE"
UNSUPPORTED = "UNSUPPORTED"


@dataclass
class AutoSupportStatus:
    company: str
    period_from: str
    period_to: str
    reporting_standard: str
    status: str
    source_package_status: str
    document_verification_status: str
    parser_status: str
    fact_coverage_status: str
    metric_coverage_status: str
    automated_quality_score: float
    manual_action_required: bool = False
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    supported_outputs: dict[str, bool] = field(default_factory=dict)
    supported_output_scopes: list[str] = field(default_factory=list)
    unsupported_output_scopes: list[str] = field(default_factory=list)
    scope_statuses: dict[str, dict[str, Any]] = field(default_factory=dict)
    source_quality_score: float = 0.0
    parser_quality_score: float = 0.0
    metric_quality_score: float = 0.0
    consistency_check_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "company": self.company,
            "period_from": self.period_from,
            "period_to": self.period_to,
            "reporting_standard": self.reporting_standard,
            "status": self.status,
            "source_package_status": self.source_package_status,
            "document_verification_status": self.document_verification_status,
            "parser_status": self.parser_status,
            "fact_coverage_status": self.fact_coverage_status,
            "metric_coverage_status": self.metric_coverage_status,
            "automated_quality_score": self.automated_quality_score,
            "manual_action_required": self.manual_action_required,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "supported_outputs": self.supported_outputs,
            "supported_output_scopes": self.supported_output_scopes,
            "unsupported_output_scopes": self.unsupported_output_scopes,
            "scope_statuses": self.scope_statuses,
            "source_quality_score": self.source_quality_score,
            "parser_quality_score": self.parser_quality_score,
            "metric_quality_score": self.metric_quality_score,
            "consistency_check_summary": self.consistency_check_summary,
        }


def evaluate_auto_support_status(
    company: str,
    period_from: str,
    period_to: str,
    reporting_standard: str,
    source_package_status: str,
    documents: list[ReportDocument],
    facts: list[StatementFact],
    metrics: list[MetricValue] | list[dict[str, Any]],
    manual_verification: dict[str, Any] | None = None,
    data_mode: str = "real",
) -> AutoSupportStatus:
    periods = periods_between(period_from, period_to)
    doc_results = [DocumentValidator().validate(document).to_dict() for document in documents]
    source_quality_score = _average([item["confidence_score"] for item in doc_results])
    document_status = _document_status(source_package_status, documents, doc_results)
    parser = evaluate_parser_quality(facts, documents, periods=periods)
    metric = evaluate_metric_quality(metrics, data_mode=data_mode)
    consistency = run_financial_consistency_checks(facts, [item for item in metrics if isinstance(item, MetricValue)]).to_dict()
    scopes = evaluate_readiness_scopes(
        parser_status=parser.status,
        document_status=document_status,
        metric_result=metric,
        consistency_summary=consistency,
        manual_verification=manual_verification,
        source_package_status=source_package_status,
    )
    blockers = []
    warnings = []
    blockers.extend(_document_blockers(document_status, source_package_status, documents, doc_results))
    blockers.extend(parser.blockers)
    blockers.extend(metric.blockers)
    if consistency["failed_checks"]:
        blockers.append("Automated financial consistency checks failed.")
    warnings.extend(parser.warnings)
    warnings.extend(metric.warnings)
    warnings.extend(item for result in doc_results for item in result.get("warnings", []))
    manual = manual_verification or {}
    if manual.get("status") == "fail" or manual.get("review_pack_status") == "FAIL":
        blockers.append("Manual QA metadata reports failed checks.")
    status = _overall_status(document_status, parser.status, metric.status, blockers, parser)
    score = _weighted_score(source_quality_score, parser.score, metric.score, consistency.get("quality_penalties", 0.0))
    fact_status = "sufficient" if parser.key_fact_coverage_ratio >= 0.8 else "partial" if facts else "missing"
    scope_payload = scopes.to_dict()
    supported_outputs = {
        "financial_facts": "statement_based_financials" in scope_payload["supported_output_scopes"],
        "financial_metrics": (
            "statement_based_financials" in scope_payload["supported_output_scopes"]
            and metric.calculated_eligible_metrics_count > 0
        ),
        "market_analysis": "market_technical_analysis" in scope_payload["supported_output_scopes"],
        "peer_comparison": "peer_comparison" in scope_payload["supported_output_scopes"],
        "llm_payload": "llm_summary" in scope_payload["supported_output_scopes"],
    }
    return AutoSupportStatus(
        company=company,
        period_from=period_from,
        period_to=period_to,
        reporting_standard=reporting_standard,
        status=status,
        source_package_status=source_package_status,
        document_verification_status=document_status,
        parser_status=parser.status,
        fact_coverage_status=fact_status,
        metric_coverage_status=metric.status,
        automated_quality_score=score,
        manual_action_required=False,
        blockers=sorted(set(blockers)),
        warnings=sorted(set(warnings)),
        supported_outputs=supported_outputs,
        supported_output_scopes=scope_payload["supported_output_scopes"],
        unsupported_output_scopes=scope_payload["unsupported_output_scopes"],
        scope_statuses=scope_payload["scope_statuses"],
        source_quality_score=source_quality_score,
        parser_quality_score=parser.score,
        metric_quality_score=metric.score,
        consistency_check_summary=consistency,
    )


def _document_status(
    source_package_status: str,
    documents: list[ReportDocument],
    doc_results: list[dict[str, Any]],
) -> str:
    if source_package_status in {"NOT_READY", "SOURCE_BLOCKED"} or not documents:
        return "blocked"
    if doc_results and all(item["validation_status"] == "pass" for item in doc_results):
        return "pass"
    if doc_results and any(item["validation_status"] in {"pass", "partial"} for item in doc_results):
        return "partial"
    return "blocked"


def _document_blockers(
    document_status: str,
    source_package_status: str,
    documents: list[ReportDocument],
    doc_results: list[dict[str, Any]],
) -> list[str]:
    blockers = []
    if source_package_status in {"NOT_READY", "SOURCE_BLOCKED"}:
        blockers.append(f"Source package status is {source_package_status}.")
    if not documents:
        blockers.append("No verified/cached real documents available.")
    if document_status == "blocked" and documents:
        blockers.append("Document validation did not pass for any source document.")
    has_press_release = any(item["detected_document_role"] == "press_release" for item in doc_results)
    has_proper_financial_source = any(
        item["detected_document_role"] in {"financial_statements", "annual_report"} for item in doc_results
    )
    if has_press_release and not has_proper_financial_source:
        blockers.append("Press-release-only extraction cannot produce AUTO_READY.")
    return blockers


def _overall_status(document_status: str, parser_status: str, metric_status: str, blockers: list[str], parser_result) -> str:
    if document_status == "blocked":
        return SOURCE_BLOCKED
    if parser_status == "parser_blocked":
        return PARSER_BLOCKED
    if parser_result.high_confidence_fact_ratio < 0.7 or parser_result.conflicting_fact_count:
        return LOW_CONFIDENCE
    if blockers:
        return AUTO_PARTIAL if parser_result.canonical_facts_count else UNSUPPORTED
    if parser_status == "pass" and metric_status in {"pass", "partial"}:
        return AUTO_PARTIAL
    return AUTO_PARTIAL


def _weighted_score(source_score: float, parser_score: float, metric_score: float, penalty: float) -> float:
    return round(max(0.0, min(1.0, source_score * 0.25 + parser_score * 0.45 + metric_score * 0.30 - penalty)), 4)


def _average(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 4)
