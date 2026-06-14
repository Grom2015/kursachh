from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Company, ReportDocument
from app.db.session import SessionLocal
from app.services.company_registry import CompanyRegistry
from app.services.company_source_discovery import CompanySourceDiscoveryService
from app.services.demo.demo_pipeline_orchestrator import DemoPipelineOrchestrator, DemoPipelineRequest
from app.services.llm.llm_analysis_payload_builder import LLMAnalysisPayloadBuilder, LLMAnalysisPayloadRequest
from app.services.market.market_audit import run_market_technical_report, save_market_technical_report
from app.services.metrics.financial_ratios_calculator import FinancialRatiosCalculator, FinancialRatiosRequest
from app.services.parsing.dataframe_fact_comparison import compare_dataframe_facts_to_existing
from app.services.parsing.dataframe_statement_parser import DataFrameStatementParser
from app.services.parsing.statement_table_extractor import StatementTableExtractor
from app.services.peers.strict_peer_analysis import PeerAnalysisRequest, StrictPeerAnalysisService
from app.services.periods import period_or_year_in_range
from app.services.reports.document_validator import DocumentValidator
from app.services.reports.downloader import ReportDownloader
from app.services.reports.financial_report_discovery import (
    FinancialReportDiscoveryRequest,
    FinancialReportDiscoveryService,
)
from app.services.review.fact_candidate_review import (
    FactCandidatePromotionRequest,
    FactCandidateReviewRequest,
    FactCandidateReviewService,
)
from app.tools.compare_dataframe_facts_to_existing import save_report as save_comparison_report
from app.tools.extract_statement_tables import build_extraction_report, save_extraction_report
from app.tools.parse_statement_tables_to_facts import save_parse_report


@dataclass
class PipelineRunRequest:
    ticker: str
    period_from: str
    period_to: str
    reporting_standard: str = "IFRS"
    board: str = "TQBR"

    def __post_init__(self) -> None:
        self.period_from, self.period_to = normalize_ui_period_range(self.period_from, self.period_to)


@dataclass
class SourcePipelineRunRequest:
    ticker: str
    period_from: str
    period_to: str
    reporting_standard: str = "IFRS"
    live_discovery: bool = False
    allow_document_download: bool = False
    run_table_extraction: bool = True
    run_dataframe_fact_parser: bool = True
    allow_text_fallback_semantic_gate: bool = True
    auto_bootstrap_identity: bool = True

    def __post_init__(self) -> None:
        self.period_from, self.period_to = normalize_ui_period_range(self.period_from, self.period_to)


@dataclass
class FullPipelineRunRequest:
    ticker: str
    period_from: str
    period_to: str
    reporting_standard: str = "IFRS"
    board: str = "TQBR"
    live_discovery: bool = True
    allow_document_download: bool = False
    allow_market_live: bool = False
    run_table_extraction: bool = True
    run_dataframe_fact_parser: bool = True
    allow_text_fallback_semantic_gate: bool = True
    auto_bootstrap_identity: bool = True

    def __post_init__(self) -> None:
        self.period_from, self.period_to = normalize_ui_period_range(self.period_from, self.period_to)


def normalize_ui_period_range(period_from: str, period_to: str) -> tuple[str, str]:
    normalized_from = str(period_from or "").strip().upper()
    normalized_to = str(period_to or "").strip().upper()
    if normalized_from.isdigit() and len(normalized_from) == 4:
        normalized_from = f"{normalized_from}Q1"
    if normalized_to.isdigit() and len(normalized_to) == 4:
        normalized_to = f"{normalized_to}Q4"
    return normalized_from, normalized_to


@dataclass
class PipelineStageResult:
    stage: str
    status: str
    action_taken: str
    report_path: str | None = None
    reason: str | None = None
    recommended_action: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class PipelineRunResult:
    ticker: str
    period_from: str
    period_to: str
    reporting_standard: str
    overall_status: str
    stages: list[PipelineStageResult]
    report_paths: dict[str, str | None]
    warnings: list[str] = field(default_factory=list)
    safety: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PipelineJobEvent:
    sequence: int
    timestamp: str
    stage: str
    status: str
    message: str
    reason: str | None = None
    recommended_action: str | None = None
    report_path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PipelineJob:
    job_id: str
    request: FullPipelineRunRequest
    status: str = "queued"
    overall_status: str | None = None
    current_stage: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    events: list[PipelineJobEvent] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["events"] = [event.to_dict() for event in self.events]
        return payload


class FullPipelineJobStore:
    def __init__(self):
        self._jobs: dict[str, PipelineJob] = {}
        self._lock = Lock()

    def create(self, request: FullPipelineRunRequest) -> PipelineJob:
        job = PipelineJob(job_id=str(uuid4()), request=request)
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> PipelineJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def add_event(
        self,
        job_id: str,
        stage: str,
        status: str,
        message: str,
        reason: str | None = None,
        recommended_action: str | None = None,
        report_path: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs[job_id]
            event = PipelineJobEvent(
                sequence=len(job.events) + 1,
                timestamp=datetime.now(UTC).isoformat(),
                stage=stage,
                status=status,
                message=message,
                reason=reason,
                recommended_action=recommended_action,
                report_path=report_path,
                details=details or {},
            )
            job.events.append(event)
            job.current_stage = stage

    def update(self, job_id: str, **kwargs: Any) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for key, value in kwargs.items():
                setattr(job, key, value)


PIPELINE_JOB_STORE = FullPipelineJobStore()


def run_full_pipeline_job(job_id: str) -> None:
    job = PIPELINE_JOB_STORE.get(job_id)
    if not job:
        return
    started = datetime.now(UTC)
    PIPELINE_JOB_STORE.update(job_id, status="running", started_at=started.isoformat())
    PIPELINE_JOB_STORE.add_event(job_id, "full_pipeline", "started", "Full company pipeline started.")
    try:
        with SessionLocal() as db:
            result = PipelineControlCenter(db).run_full_company_pipeline(job.request, job_id=job_id)
        finished = datetime.now(UTC)
        PIPELINE_JOB_STORE.update(
            job_id,
            status="completed",
            overall_status=result.overall_status,
            finished_at=finished.isoformat(),
            duration_ms=int((finished - started).total_seconds() * 1000),
            result=result.to_dict(),
        )
        PIPELINE_JOB_STORE.add_event(
            job_id,
            "full_pipeline",
            result.overall_status,
            f"Full company pipeline finished with {result.overall_status}.",
        )
    except Exception as exc:
        finished = datetime.now(UTC)
        PIPELINE_JOB_STORE.update(
            job_id,
            status="failed",
            overall_status="BLOCKED",
            finished_at=finished.isoformat(),
            duration_ms=int((finished - started).total_seconds() * 1000),
            error_message=str(exc),
        )
        PIPELINE_JOB_STORE.add_event(job_id, "full_pipeline", "BLOCKED", "Full company pipeline failed.", reason=str(exc))


def summarize_engine_contribution(report: dict[str, Any]) -> dict[str, Any]:
    structured_facts = list(report.get("structured_facts") or [])
    rejected_rows = list(report.get("rejected_rows") or [])
    unmapped_numeric_evidence = list(report.get("unmapped_numeric_evidence") or [])
    unmapped_table_evidence = list(report.get("unmapped_table_evidence") or [])
    fact_contribution_by_engine = contribution_counts(structured_facts)
    evidence_items = [*rejected_rows, *unmapped_numeric_evidence, *unmapped_table_evidence]
    evidence_contribution_by_engine = contribution_counts(evidence_items)
    return {
        "merged_fact_count": sum(1 for fact in structured_facts if fact.get("fusion_status") == "merged_engines"),
        "ocr_only_fact_count": sum(1 for fact in structured_facts if is_ocr_only_item(fact)),
        "merged_ocr_fact_count": sum(
            1
            for fact in structured_facts
            if fact.get("fusion_status") == "merged_engines"
            and any("ocr" in str(engine) for engine in list(fact.get("source_engines_involved") or []))
        ),
        "fact_contribution_by_engine": fact_contribution_by_engine,
        "evidence_contribution_by_engine": evidence_contribution_by_engine,
        "engines_with_evidence_only_contribution": sorted(
            engine
            for engine in evidence_contribution_by_engine
            if engine not in fact_contribution_by_engine
        ),
    }


def contribution_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        engines = list(item.get("source_engines_involved") or [])
        if not engines and item.get("source_engine"):
            engines = [str(item.get("source_engine"))]
        for engine in engines:
            normalized = str(engine or "").strip()
            if not normalized:
                continue
            counts[normalized] = counts.get(normalized, 0) + 1
    return counts


def is_ocr_only_item(item: dict[str, Any]) -> bool:
    engines = list(item.get("source_engines_involved") or [])
    if not engines and item.get("source_engine"):
        engines = [str(item.get("source_engine"))]
    return bool(engines) and all("ocr" in str(engine) for engine in engines)


class PipelineControlCenter:
    def __init__(self, db: Session, root: Path | None = None):
        self.db = db
        self.root = (root or get_settings().root_dir).resolve()

    def run_safe_report_pipeline(self, request: PipelineRunRequest) -> PipelineRunResult:
        ticker = request.ticker.upper()
        stages = [
            self._run_financial_ratios(request),
            self._run_market_technical(request),
            self._run_peer_analysis(request),
            self._run_llm_payload(request),
            self._run_demo_report(request),
        ]
        report_paths = {stage.stage: stage.report_path for stage in stages}
        return PipelineRunResult(
            ticker=ticker,
            period_from=request.period_from,
            period_to=request.period_to,
            reporting_standard=request.reporting_standard.upper(),
            overall_status=self._overall_status(stages),
            stages=stages,
            report_paths=report_paths,
            warnings=[warning for stage in stages for warning in stage.warnings],
            safety={
                "facts_persisted": False,
                "metric_engine_invoked": False,
                "valuation_invoked": False,
                "peer_live_fetch_invoked": False,
                "llm_invoked": False,
                "market_live_fetch_invoked": False,
                "manifest_mutated": False,
            },
        )

    def run_draft_candidate_analysis(self, request: PipelineRunRequest) -> PipelineRunResult:
        """Build a full draft analysis from existing DataFrame candidates without persisting facts."""
        ticker = request.ticker.upper()
        stages: list[PipelineStageResult] = []
        company = CompanyRegistry(self.db).resolve_one(ticker)
        if company:
            stages.append(
                self._parse_dataframe_facts(
                    company,
                    SourcePipelineRunRequest(
                        ticker=ticker,
                        period_from=request.period_from,
                        period_to=request.period_to,
                        reporting_standard=request.reporting_standard,
                        run_table_extraction=False,
                        run_dataframe_fact_parser=True,
                        allow_text_fallback_semantic_gate=True,
                    ),
                )
            )
        else:
            stages.append(
                PipelineStageResult(
                    stage="dataframe_fact_parse",
                    status="BLOCKED",
                    action_taken="parsed DataFrame fact candidates, not persisted",
                    reason="company_not_in_registry",
                    recommended_action="run_full_pipeline_or_upload_report",
                )
            )
        stages.extend(
            [
                self._run_draft_financial_ratios(request),
                self._run_peer_analysis(request),
                self._run_llm_payload(request),
                self._run_demo_report(request),
            ]
        )
        report_paths = {stage.stage: stage.report_path for stage in stages}
        return PipelineRunResult(
            ticker=ticker,
            period_from=request.period_from,
            period_to=request.period_to,
            reporting_standard=request.reporting_standard.upper(),
            overall_status=self._overall_status(stages),
            stages=stages,
            report_paths=report_paths,
            warnings=[warning for stage in stages for warning in stage.warnings],
            safety={
                "analysis_trust_level": "manual_upload_candidate_based",
                "facts_source": "dataframe_parse_report",
                "text_fallback_candidates_allowed": True,
                "facts_persisted": False,
                "metric_engine_invoked": False,
                "valuation_invoked": False,
                "peer_live_fetch_invoked": False,
                "llm_invoked": False,
                "market_live_fetch_invoked": False,
                "manifest_mutated": False,
            },
        )

    def build_fact_candidate_review(self, request: PipelineRunRequest) -> PipelineRunResult:
        review, path = FactCandidateReviewService(self.db, root=self.root).build_review_report(
            FactCandidateReviewRequest(
                company_ticker=request.ticker,
                period_from=request.period_from,
                period_to=request.period_to,
                reporting_standard=request.reporting_standard,
            )
        )
        status = "PASS" if review.eligible_count else "BLOCKED" if not review.candidates_count else "PARTIAL"
        stage = PipelineStageResult(
            stage="fact_candidate_review",
            status=status,
            action_taken="built review report, facts not persisted",
            report_path=self._relative(path),
            reason=None if review.eligible_count else "no_eligible_candidates",
            recommended_action=(
                "review_and_confirm_candidates" if review.eligible_count else "rerun_draft_or_upload_better_report"
            ),
            summary={
                "candidates_count": review.candidates_count,
                "eligible_count": review.eligible_count,
                "needs_review_count": review.needs_review_count,
                "blocked_count": review.blocked_count,
                "facts_persisted": False,
            },
            warnings=review.warnings,
        )
        return PipelineRunResult(
            ticker=request.ticker.upper(),
            period_from=request.period_from,
            period_to=request.period_to,
            reporting_standard=request.reporting_standard.upper(),
            overall_status=status,
            stages=[stage],
            report_paths={stage.stage: stage.report_path},
            warnings=review.warnings,
            safety=review.safety,
        )

    def promote_reviewed_candidates(self, request: PipelineRunRequest, confirm: bool = False) -> PipelineRunResult:
        result, path = FactCandidateReviewService(self.db, root=self.root).promote_reviewed_candidates(
            FactCandidatePromotionRequest(
                company_ticker=request.ticker,
                period_from=request.period_from,
                period_to=request.period_to,
                reporting_standard=request.reporting_standard,
                confirm=confirm,
            )
        )
        stage = PipelineStageResult(
            stage="fact_candidate_promotion",
            status="PASS" if result["promoted_count"] else "BLOCKED",
            action_taken="promoted reviewed manual candidates",
            report_path=self._relative(path),
            reason=None if result["promoted_count"] else "no_candidates_promoted",
            recommended_action="calculate_ratios_from_persisted_facts" if result["promoted_count"] else "review_candidates_first",
            summary={
                "promoted_count": result["promoted_count"],
                "skipped_count": result["skipped_count"],
                "blocked_count": result["blocked_count"],
                "quality_flag": "reviewed_manual_upload",
                "official_source_verified": False,
                "source_package_ready_contribution": False,
            },
        )
        return PipelineRunResult(
            ticker=request.ticker.upper(),
            period_from=request.period_from,
            period_to=request.period_to,
            reporting_standard=request.reporting_standard.upper(),
            overall_status=stage.status,
            stages=[stage],
            report_paths={stage.stage: stage.report_path},
            safety=result["safety"],
        )

    def run_source_discovery_pipeline(self, request: SourcePipelineRunRequest) -> PipelineRunResult:
        ticker = request.ticker.upper()
        stages: list[PipelineStageResult] = []
        company = CompanyRegistry(self.db).resolve_one(ticker)
        if company:
            stages.append(
                PipelineStageResult(
                    stage="identity_resolution",
                    status="PASS",
                    action_taken="resolved from local registry",
                    summary={"ticker": company.ticker, "company_name": company.full_name, "sector": company.sector},
                )
            )
        else:
            source_service = CompanySourceDiscoveryService(self.db, root=self.root)
            source_report = source_service.discover(ticker, ticker=ticker, live=request.live_discovery)
            source_path = source_service.save_report(source_report)
            company = self._bootstrap_company_from_identity(ticker, source_report) if request.auto_bootstrap_identity else None
            if company:
                stages.append(
                    PipelineStageResult(
                        stage="identity_resolution",
                        status="PASS",
                        action_taken="bootstrapped identity from MOEX ISS exact ticker match",
                        report_path=self._relative(source_path),
                        summary={
                            "ticker": company.ticker,
                            "company_name": company.full_name,
                            "board": company.board,
                            "trust_scope": "identity_only",
                        },
                        warnings=[
                            "identity_bootstrapped_from_moex_iss",
                            "MOEX identity metadata is not a source of financial statements.",
                        ]
                        + source_report.warnings,
                    )
                )
            else:
                stages.append(
                    PipelineStageResult(
                        stage="identity_resolution",
                        status="BLOCKED",
                        action_taken="checked local registry and source candidates",
                        report_path=self._relative(source_path),
                        reason="company_not_in_registry",
                        recommended_action="add_company_to_registry_or_use_manual_upload",
                        summary={
                            "recommended_candidates_count": len(source_report.recommended_candidates),
                            "auto_select_allowed": source_report.auto_select_allowed,
                        },
                        warnings=source_report.warnings,
                    )
                )
                return self._source_pipeline_result(request, stages)

        source_report = CompanySourceDiscoveryService(self.db, root=self.root).discover(
            ticker,
            ticker=ticker,
            live=request.live_discovery,
        )
        source_path = CompanySourceDiscoveryService(self.db, root=self.root).save_report(source_report)
        stages.append(
            PipelineStageResult(
                stage="source_discovery",
                status="PASS" if source_report.recommended_candidates else "PARTIAL",
                action_taken="discovered source candidates",
                report_path=self._relative(source_path),
                summary={
                    "recommended_candidates_count": len(source_report.recommended_candidates),
                    "auto_select_allowed": source_report.auto_select_allowed,
                },
                warnings=source_report.warnings,
            )
        )

        report_service = FinancialReportDiscoveryService(self.db, root=self.root)
        discovery_report = report_service.discover(
            FinancialReportDiscoveryRequest(
                company_query=ticker,
                ticker=ticker,
                period_from=request.period_from,
                period_to=request.period_to,
                reporting_standard=request.reporting_standard,
                live=request.live_discovery,
            )
        )
        discovery_path = report_service.save_report(discovery_report)
        stages.append(
            PipelineStageResult(
                stage="report_discovery",
                status=self._discovery_status(discovery_report.status),
                action_taken="discovered report candidates",
                report_path=self._relative(discovery_path),
                reason=None if discovery_report.status == "READY" else "missing_report_candidates",
                recommended_action=None if discovery_report.status == "READY" else "use_manual_upload_or_enable_live_discovery",
                summary={
                    "status": discovery_report.status,
                    "discovered_reports_count": len(discovery_report.discovered_reports),
                    "missing_periods": discovery_report.missing_periods,
                },
                warnings=discovery_report.warnings,
            )
        )

        documents: list[ReportDocument] = []
        if request.allow_document_download:
            documents, download_stage = self._download_documents(company, discovery_report.discovered_reports)
            stages.append(download_stage)
        else:
            stages.append(
                PipelineStageResult(
                    stage="download_documents",
                    status="SKIPPED",
                    action_taken="not executed",
                    reason="document_download_not_enabled",
                    recommended_action="enable_document_download_explicitly",
                )
            )
            documents = self._existing_cached_documents(company, request)

        if documents:
            stages.append(self._validate_documents(documents))
        else:
            stages.append(
                PipelineStageResult(
                    stage="document_validation",
                    status="BLOCKED",
                    action_taken="not executed",
                    reason="no_cached_or_downloaded_documents",
                    recommended_action="download_documents_or_manual_upload",
                )
            )

        if request.run_table_extraction and documents:
            stages.append(self._extract_statement_tables(company, request, documents))
        else:
            stages.append(
                PipelineStageResult(
                    stage="statement_table_extraction",
                    status="SKIPPED" if not request.run_table_extraction else "BLOCKED",
                    action_taken="not executed",
                    reason="table_extraction_not_enabled" if not request.run_table_extraction else "no_documents",
                )
            )

        if request.run_dataframe_fact_parser and documents:
            stages.append(self._parse_dataframe_facts(company, request))
        else:
            stages.append(
                PipelineStageResult(
                    stage="dataframe_fact_parse",
                    status="SKIPPED" if not request.run_dataframe_fact_parser else "BLOCKED",
                    action_taken="not executed",
                    reason="fact_parser_not_enabled" if not request.run_dataframe_fact_parser else "no_documents",
                )
            )
        return self._source_pipeline_result(request, stages)

    def run_full_company_pipeline(self, request: FullPipelineRunRequest, job_id: str | None = None) -> PipelineRunResult:
        stages: list[PipelineStageResult] = []
        self._emit(job_id, "source_pipeline", "started", "Source pipeline started.")
        source_result = self.run_source_discovery_pipeline(
            SourcePipelineRunRequest(
                ticker=request.ticker,
                period_from=request.period_from,
                period_to=request.period_to,
                reporting_standard=request.reporting_standard,
                live_discovery=request.live_discovery,
                allow_document_download=request.allow_document_download,
                run_table_extraction=request.run_table_extraction,
                run_dataframe_fact_parser=request.run_dataframe_fact_parser,
                allow_text_fallback_semantic_gate=request.allow_text_fallback_semantic_gate,
                auto_bootstrap_identity=request.auto_bootstrap_identity,
            )
        )
        stages.extend(source_result.stages)
        for stage in source_result.stages:
            self._emit_stage(job_id, stage)
        if any(stage.stage == "report_discovery" and stage.status == "BLOCKED" for stage in source_result.stages):
            self._emit(
                job_id,
                "manual_upload_needed",
                "recommended",
                "Official report was not found automatically; manual upload fallback is available.",
                reason="official_report_not_found",
                recommended_action="upload_report_manually",
            )

        parse_stage = next((stage for stage in source_result.stages if stage.stage == "dataframe_fact_parse"), None)
        if parse_stage and parse_stage.report_path:
            self._emit(job_id, "audit_comparison", "started", "Audit-only comparison started.")
            stages.append(self._run_fact_comparison(request))
            self._emit_stage(job_id, stages[-1])

        self._emit(job_id, "financial_ratios", "started", "Financial ratios report generation started.")
        stages.append(self._run_financial_ratios(PipelineRunRequest(**self._safe_request_kwargs(request))))
        self._emit_stage(job_id, stages[-1])

        self._emit(job_id, "market_technical", "started", "Market technical report started.")
        if request.allow_market_live:
            stages.append(self._run_market_technical_live(request))
        else:
            stages.append(self._run_market_technical(PipelineRunRequest(**self._safe_request_kwargs(request))))
        self._emit_stage(job_id, stages[-1])

        self._emit(job_id, "peer_analysis", "started", "Peer analysis from existing ratios started.")
        stages.append(self._run_peer_analysis(PipelineRunRequest(**self._safe_request_kwargs(request))))
        self._emit_stage(job_id, stages[-1])

        self._emit(job_id, "llm_payload", "started", "LLM analysis payload build started; LLM will not be invoked.")
        stages.append(self._run_llm_payload(PipelineRunRequest(**self._safe_request_kwargs(request))))
        self._emit_stage(job_id, stages[-1])

        self._emit(job_id, "demo_report", "started", "Demo report generation started.")
        stages.append(self._run_demo_report(PipelineRunRequest(**self._safe_request_kwargs(request))))
        self._emit_stage(job_id, stages[-1])

        return PipelineRunResult(
            ticker=request.ticker.upper(),
            period_from=request.period_from,
            period_to=request.period_to,
            reporting_standard=request.reporting_standard.upper(),
            overall_status=self._overall_status(stages),
            stages=stages,
            report_paths={stage.stage: stage.report_path for stage in stages},
            warnings=[warning for stage in stages for warning in stage.warnings],
            safety={
                "facts_persisted": False,
                "metric_engine_invoked": False,
                "valuation_invoked": False,
                "peer_live_fetch_invoked": False,
                "llm_invoked": False,
                "manifest_mutated": False,
                "document_download_enabled": request.allow_document_download,
                "live_discovery_enabled": request.live_discovery,
                "market_live_fetch_enabled": request.allow_market_live,
            },
        )

    def status(self, ticker: str, period_from: str, period_to: str, reporting_standard: str = "IFRS") -> dict[str, Any]:
        ticker = ticker.upper()
        paths = {
            "machine_report": self.root / "data" / "validation" / ticker / f"{period_to}_machine_report.json",
            "financial_ratios": self._company_report_path(ticker, period_from, period_to, "financial_ratios"),
            "market_technical": self._company_report_path(ticker, period_from, period_to, "market_technical_report"),
            "peer_analysis": self.root
            / "data"
            / "validation"
            / "PEERS"
            / f"{ticker}_{period_from}_{period_to}_peer_analysis_report.json",
            "llm_payload": self._company_report_path(ticker, period_from, period_to, "llm_analysis_payload"),
            "fact_candidate_review": self._company_report_path(ticker, period_from, period_to, "fact_candidate_review"),
            "fact_candidate_promotion": self._company_report_path(
                ticker, period_from, period_to, "fact_candidate_promotion"
            ),
            "demo_report": self.root / "data" / "validation" / "demo" / "demo_pipeline_report.json",
            "manual_ingestion": self.root / "data" / "validation" / ticker / f"{period_to}_manual_report_ingestion.json",
        }
        reports = {name: self._load_json(path) for name, path in paths.items()}
        return {
            "ticker": ticker,
            "period_from": period_from,
            "period_to": period_to,
            "reporting_standard": reporting_standard.upper(),
            "reports": [
                {
                    "name": name,
                    "exists": path.exists(),
                    "path": self._relative(path),
                    "summary": self._status_summary(name, reports[name]),
                }
                for name, path in paths.items()
            ],
            "safety": {
                "read_only_status_check": True,
                "artifacts_created": False,
                "facts_persisted": False,
                "metric_engine_invoked": False,
                "llm_invoked": False,
            },
        }

    def _run_financial_ratios(self, request: PipelineRunRequest) -> PipelineStageResult:
        try:
            report = FinancialRatiosCalculator(self.db, root=self.root).calculate(
                FinancialRatiosRequest(
                    company_ticker=request.ticker,
                    period_from=request.period_from,
                    period_to=request.period_to,
                    reporting_standard=request.reporting_standard,
                    source="persisted_facts",
                    allow_text_fallback_candidates=False,
                )
            )
            path = FinancialRatiosCalculator(self.db, root=self.root).save_report(report)
            summary = report.summary
            status = "PASS" if summary.get("calculated_count", 0) > 0 else "PARTIAL"
            return PipelineStageResult(
                stage="financial_ratios",
                status=status,
                action_taken="generated",
                report_path=self._relative(path),
                summary=summary,
                warnings=report.warnings,
            )
        except Exception as exc:
            return self._blocked("financial_ratios", "generated", exc)

    def _run_draft_financial_ratios(self, request: PipelineRunRequest) -> PipelineStageResult:
        try:
            report = FinancialRatiosCalculator(self.db, root=self.root).calculate(
                FinancialRatiosRequest(
                    company_ticker=request.ticker,
                    period_from=request.period_from,
                    period_to=request.period_to,
                    reporting_standard=request.reporting_standard,
                    source="dataframe_parse_report",
                    allow_text_fallback_candidates=True,
                )
            )
            report.warnings = [
                *report.warnings,
                "draft_analysis_uses_dataframe_fact_candidates",
                "facts_not_persisted",
                "metric_uses_text_fallback_fact_candidates",
            ]
            report.methodology_notes = [
                *report.methodology_notes,
                "Draft analysis uses DataFrame fact candidates from uploaded/extracted reports.",
                "Facts are not persisted as official StatementFact rows.",
            ]
            path = FinancialRatiosCalculator(self.db, root=self.root).save_report(report)
            calculated_metrics = [
                {
                    "metric_code": metric.get("metric_code"),
                    "period": metric.get("period"),
                    "display_value": metric.get("display_value"),
                    "value": metric.get("value"),
                    "trust_warning": metric.get("trust_warning"),
                }
                for metric in report.metrics
                if metric.get("status") == "calculated"
            ]
            unavailable_metrics = [
                {
                    "metric_code": metric.get("metric_code"),
                    "period": metric.get("period"),
                    "status": metric.get("status"),
                    "reason": metric.get("reason"),
                    "inputs_missing": metric.get("inputs_missing"),
                }
                for metric in report.metrics
                if metric.get("status") != "calculated"
            ][:12]
            summary = {
                **report.summary,
                "analysis_trust_level": "manual_upload_candidate_based",
                "facts_source": "dataframe_parse_report",
                "facts_persisted": False,
                "text_fallback_candidates_allowed": True,
                "calculated_metrics": calculated_metrics,
                "unavailable_metrics_sample": unavailable_metrics,
                "trust_warning": "metric_uses_text_fallback_fact_candidates",
            }
            status = "PASS" if report.summary.get("calculated_count", 0) > 0 else "PARTIAL"
            return PipelineStageResult(
                stage="financial_ratios",
                status=status,
                action_taken="generated from DataFrame candidates",
                report_path=self._relative(path),
                reason=None if status == "PASS" else "no_calculated_ratios_from_candidates",
                recommended_action=None if status == "PASS" else "upload_better_statement_or_review_candidate_facts",
                summary=summary,
                warnings=report.warnings,
            )
        except Exception as exc:
            return self._blocked(
                "financial_ratios",
                "generated from DataFrame candidates",
                exc,
                "run_full_pipeline_or_upload_report_with_fact_parser",
            )

    def _run_market_technical(self, request: PipelineRunRequest) -> PipelineStageResult:
        try:
            report = run_market_technical_report(
                ticker=request.ticker,
                period_from=request.period_from,
                period_to=request.period_to,
                board=request.board,
                mode="replay-cache",
                cache_root=self.root / "data" / "market_cache",
            )
            path = save_market_technical_report(report)
            warnings = list(report.get("warnings") or [])
            if report.get("status") == "FAIL" and any("Cached market data not found" in item for item in warnings):
                return PipelineStageResult(
                    stage="market_technical",
                    status="BLOCKED",
                    action_taken="read from replay-cache",
                    report_path=self._relative(path),
                    reason="cache_missing",
                    recommended_action="run_market_live_explicitly",
                    summary=report.get("summary") or {},
                    warnings=warnings,
                )
            return PipelineStageResult(
                stage="market_technical",
                status="PASS" if report.get("status") == "PASS" else "PARTIAL",
                action_taken="read from replay-cache",
                report_path=self._relative(path),
                summary=report.get("summary") or {},
                warnings=warnings,
            )
        except Exception as exc:
            return self._blocked("market_technical", "read from replay-cache", exc, "run_market_live_explicitly")

    def _run_market_technical_live(self, request: FullPipelineRunRequest) -> PipelineStageResult:
        try:
            report = run_market_technical_report(
                ticker=request.ticker,
                period_from=request.period_from,
                period_to=request.period_to,
                board=request.board,
                mode="live",
                cache_root=self.root / "data" / "market_cache",
            )
            path = save_market_technical_report(report)
            return PipelineStageResult(
                stage="market_technical",
                status="PASS" if report.get("status") == "PASS" else "PARTIAL" if report.get("status") != "FAIL" else "BLOCKED",
                action_taken="fetched live MOEX ISS candles explicitly",
                report_path=self._relative(path),
                reason=None if report.get("status") != "FAIL" else "market_live_fetch_failed",
                summary=report.get("summary") or {},
                warnings=list(report.get("warnings") or []),
            )
        except Exception as exc:
            return self._blocked("market_technical", "fetched live MOEX ISS candles explicitly", exc)

    def _run_peer_analysis(self, request: PipelineRunRequest) -> PipelineStageResult:
        try:
            service = StrictPeerAnalysisService(self.db, root=self.root)
            report = service.build(
                PeerAnalysisRequest(
                    ticker=request.ticker,
                    period_from=request.period_from,
                    period_to=request.period_to,
                    reporting_standard=request.reporting_standard,
                    max_peers=5,
                )
            )
            path = service.save_report(report)
            readiness = report.comparison_readiness
            status = "PASS" if readiness == "READY_WITH_LIMITATIONS" else "PARTIAL" if readiness == "PARTIAL" else "BLOCKED"
            return PipelineStageResult(
                stage="peer_analysis",
                status=status,
                action_taken="generated from existing ratios",
                report_path=self._relative(path),
                reason=None if status != "BLOCKED" else "peer_comparison_not_ready",
                summary=report.summary,
                warnings=report.warnings,
            )
        except Exception as exc:
            return self._blocked("peer_analysis", "generated from existing ratios", exc)

    def _run_llm_payload(self, request: PipelineRunRequest) -> PipelineStageResult:
        try:
            builder = LLMAnalysisPayloadBuilder(root=self.root)
            payload = builder.build(
                LLMAnalysisPayloadRequest(
                    company_ticker=request.ticker,
                    period_from=request.period_from,
                    period_to=request.period_to,
                    reporting_standard=request.reporting_standard,
                    include_provider_strategy=False,
                )
            )
            path = builder.save_payload(payload)
            data = payload.to_dict()
            return PipelineStageResult(
                stage="llm_payload",
                status="PASS",
                action_taken="built, LLM not invoked",
                report_path=self._relative(path),
                summary={
                    "calculated_ratios": len((data.get("financial_ratios") or {}).get("calculated") or []),
                    "blockers_count": len(data.get("blockers") or []),
                    "llm_invoked": False,
                },
                warnings=(data.get("data_quality") or {}).get("warnings") or [],
            )
        except Exception as exc:
            return self._blocked("llm_payload", "built, LLM not invoked", exc)

    def _run_demo_report(self, request: PipelineRunRequest) -> PipelineStageResult:
        try:
            orchestrator = DemoPipelineOrchestrator(root=self.root)
            report = orchestrator.build_report(
                DemoPipelineRequest(
                    tickers=[request.ticker.upper()],
                    period_from=request.period_from,
                    period_to=request.period_to,
                    reporting_standard=request.reporting_standard,
                    replay_cache=True,
                    output_formats=["json", "md", "html"],
                )
            )
            paths = orchestrator.save_report(report, output_formats=["json", "md", "html"])
            return PipelineStageResult(
                stage="demo_report",
                status="PASS",
                action_taken="generated",
                report_path=self._relative(Path(paths["json"])),
                summary={
                    "markdown_report_path": self._relative(Path(paths["md"])),
                    "html_report_path": self._relative(Path(paths["html"])),
                },
                warnings=report.limitations,
            )
        except Exception as exc:
            return self._blocked("demo_report", "generated", exc)

    def _run_fact_comparison(self, request: FullPipelineRunRequest) -> PipelineStageResult:
        try:
            report = compare_dataframe_facts_to_existing(
                self.db,
                request.ticker,
                request.period_from,
                request.period_to,
                reporting_standard=request.reporting_standard,
                replay_cache=True,
                allow_text_fallback_semantic_gate=request.allow_text_fallback_semantic_gate,
            )
            path = save_comparison_report(report)
            conflicts = int(report.get("conflict_count") or 0)
            return PipelineStageResult(
                stage="audit_comparison",
                status="BLOCKED" if conflicts else "PASS" if report.get("dataframe_candidates_count") else "PARTIAL",
                action_taken="compared DataFrame candidates to existing facts, audit-only",
                report_path=self._relative(path),
                reason="fact_value_conflicts" if conflicts else None,
                summary={
                    "dataframe_candidates_count": report.get("dataframe_candidates_count"),
                    "matched_count": report.get("matched_count"),
                    "conflict_count": conflicts,
                    "safe_to_persist_in_this_stage": False,
                },
            )
        except Exception as exc:
            return self._blocked("audit_comparison", "compared DataFrame candidates to existing facts, audit-only", exc)

    def _download_documents(self, company: Company, reports: list[Any]) -> tuple[list[ReportDocument], PipelineStageResult]:
        documents: list[ReportDocument] = []
        warnings: list[str] = []
        for report in reports:
            try:
                documents.append(ReportDownloader(self.db).download(company, report.to_dict()))
            except Exception as exc:
                warnings.append(str(exc))
        self.db.commit()
        status = "PASS" if documents else "BLOCKED"
        return documents, PipelineStageResult(
            stage="download_documents",
            status=status,
            action_taken="downloaded official report candidates",
            reason=None if documents else "no_documents_downloaded",
            recommended_action=None if documents else "manual_upload_or_provider_access_check",
            summary={"documents_downloaded": len(documents)},
            warnings=warnings,
        )

    def _bootstrap_company_from_identity(self, ticker: str, source_report: Any) -> Company | None:
        ticker = ticker.upper()
        exact_candidates = [
            candidate
            for candidate in source_report.recommended_candidates
            if candidate.source_provider == "moex_iss"
            and candidate.verification_scope == "identity_only"
            and (candidate.ticker or "").upper() == ticker
        ]
        if not exact_candidates:
            return None
        preferred = next(
            (
                candidate
                for candidate in exact_candidates
                if (candidate.provenance or {}).get("board") == "TQBR"
            ),
            exact_candidates[0],
        )
        provenance = preferred.provenance or {}
        existing = (
            self.db.query(Company)
            .filter(Company.ticker == ticker, Company.board == (provenance.get("board") or "TQBR"))
            .first()
        )
        if existing:
            return existing
        company = Company(
            ticker=ticker,
            isin=provenance.get("isin"),
            board=provenance.get("board") or "TQBR",
            short_name=provenance.get("short_name") or preferred.matched_name or ticker,
            full_name=provenance.get("name") or preferred.matched_name or ticker,
            aliases_json=[ticker],
            sector=None,
            subsector=None,
            is_active=True,
        )
        self.db.add(company)
        self.db.commit()
        self.db.refresh(company)
        return company

    def _existing_cached_documents(self, company: Company, request: SourcePipelineRunRequest) -> list[ReportDocument]:
        docs = [
            doc
            for doc in self.db.query(ReportDocument)
            .filter(
                ReportDocument.company_id == company.id,
                ReportDocument.reporting_standard == request.reporting_standard.upper(),
                ReportDocument.source_type != "fixture",
            )
            .all()
            if period_or_year_in_range(doc.report_period, request.period_from, request.period_to)
            and doc.status in {"downloaded", "validated", "parsed"}
        ]
        return docs

    def _validate_documents(self, documents: list[ReportDocument]) -> PipelineStageResult:
        warnings: list[str] = []
        passed = 0
        for document in documents:
            result = DocumentValidator().validate(document, expected_file_type=Path(document.file_name or "").suffix.lstrip("."))
            document.validation_warnings_json = result.warnings
            warnings.extend(result.warnings)
            if result.validation_status == "pass":
                document.status = "validated"
                document.rejection_reason = None
                passed += 1
            else:
                document.status = "rejected"
                document.rejection_reason = "document_validation_failed_in_source_pipeline"
        self.db.commit()
        return PipelineStageResult(
            stage="document_validation",
            status="PASS" if passed == len(documents) else "PARTIAL" if passed else "BLOCKED",
            action_taken="validated cached/downloaded documents",
            reason=None if passed else "document_validation_failed",
            summary={"documents_checked": len(documents), "documents_validated": passed},
            warnings=warnings,
        )

    def _extract_statement_tables(
        self,
        company: Company,
        request: SourcePipelineRunRequest,
        documents: list[ReportDocument],
    ) -> PipelineStageResult:
        eligible = [doc for doc in documents if doc.status in {"downloaded", "validated", "parsed"}]
        reports = [StatementTableExtractor(root=self.root).extract(document) for document in eligible]
        extraction = build_extraction_report(
            company.ticker,
            request.period_from,
            request.period_to,
            request.reporting_standard,
            reports,
        )
        path = save_extraction_report(extraction)
        count = int(extraction.get("statement_tables_count") or 0)
        return PipelineStageResult(
            stage="statement_table_extraction",
            status="PASS" if extraction.get("required_statement_tables_found") else "PARTIAL" if count else "BLOCKED",
            action_taken="extracted statement table artifacts",
            report_path=self._relative(path),
            reason=None if count else "no_statement_tables_extracted",
            summary={
                "documents_processed": extraction.get("documents_processed"),
                "statement_tables_count": count,
                "facts_extracted": 0,
                "fact_parser_status": "not_invoked",
            },
            warnings=extraction.get("warnings") or [],
        )

    def _parse_dataframe_facts(self, company: Company, request: SourcePipelineRunRequest) -> PipelineStageResult:
        result = DataFrameStatementParser(
            self.db,
            root=self.root,
            allow_text_fallback_semantic_gate=request.allow_text_fallback_semantic_gate,
        ).parse(
            company.ticker,
            request.period_from,
            request.period_to,
            request.reporting_standard,
        )
        report = result.to_dict()
        report["db_persisted"] = False
        report["persisted_facts_count"] = 0
        report["text_fallback_semantic_gate_enabled"] = request.allow_text_fallback_semantic_gate
        path = save_parse_report(report)
        candidates = int(report.get("canonical_facts_created") or 0)
        return PipelineStageResult(
            stage="dataframe_fact_parse",
            status="PASS" if candidates else "PARTIAL" if report.get("tables_processed") else "BLOCKED",
            action_taken="parsed DataFrame fact candidates, not persisted",
            report_path=self._relative(path),
            reason=None if candidates else "no_fact_candidates",
            summary={
                "documents_processed": report.get("documents_processed"),
                "tables_processed": report.get("tables_processed"),
                "canonical_fact_candidates": candidates,
                "structured_facts_count": len(report.get("structured_facts") or []),
                "derived_safe_facts_count": len(report.get("derived_safe_facts") or []),
                "rejected_rows_count": len(report.get("rejected_rows") or []),
                "unmapped_numeric_evidence_count": len(report.get("unmapped_numeric_evidence") or []),
                "unmapped_table_evidence_count": len(report.get("unmapped_table_evidence") or []),
                "llm_ready_evidence_pack_available": bool(report.get("llm_ready_evidence_pack")),
                "fact_metric_codes": sorted(
                    {str(item.get("metric_code")) for item in report.get("facts", []) if item.get("metric_code")}
                ),
                "banking_parser_quality": report.get("banking_parser_quality") or {},
                "analysis_readiness_summary": report.get("analysis_readiness_summary") or {},
                "engine_contribution_summary": summarize_engine_contribution(report),
                "db_persisted": False,
            },
            warnings=report.get("warnings") or [],
        )

    def _source_pipeline_result(
        self,
        request: SourcePipelineRunRequest,
        stages: list[PipelineStageResult],
    ) -> PipelineRunResult:
        return PipelineRunResult(
            ticker=request.ticker.upper(),
            period_from=request.period_from,
            period_to=request.period_to,
            reporting_standard=request.reporting_standard.upper(),
            overall_status=self._overall_status(stages),
            stages=stages,
            report_paths={stage.stage: stage.report_path for stage in stages},
            warnings=[warning for stage in stages for warning in stage.warnings],
            safety={
                "facts_persisted": False,
                "metric_engine_invoked": False,
                "valuation_invoked": False,
                "peer_live_fetch_invoked": False,
                "llm_invoked": False,
                "manifest_mutated": False,
                "document_download_enabled": request.allow_document_download,
                "live_discovery_enabled": request.live_discovery,
            },
        )

    def _discovery_status(self, status: str) -> str:
        if status == "READY":
            return "PASS"
        if status == "PARTIAL":
            return "PARTIAL"
        return "BLOCKED"

    def _safe_request_kwargs(self, request: FullPipelineRunRequest) -> dict[str, Any]:
        return {
            "ticker": request.ticker,
            "period_from": request.period_from,
            "period_to": request.period_to,
            "reporting_standard": request.reporting_standard,
            "board": request.board,
        }

    def _emit(
        self,
        job_id: str | None,
        stage: str,
        status: str,
        message: str,
        reason: str | None = None,
        recommended_action: str | None = None,
        report_path: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if job_id:
            PIPELINE_JOB_STORE.add_event(
                job_id,
                stage,
                status,
                message,
                reason=reason,
                recommended_action=recommended_action,
                report_path=report_path,
                details=details,
            )

    def _emit_stage(self, job_id: str | None, stage: PipelineStageResult) -> None:
        self._emit(
            job_id,
            stage.stage,
            stage.status,
            f"{stage.stage}: {stage.action_taken}",
            reason=stage.reason,
            recommended_action=stage.recommended_action,
            report_path=stage.report_path,
            details=stage.summary,
        )

    def _blocked(
        self,
        stage: str,
        action_taken: str,
        exc: Exception,
        recommended_action: str | None = None,
    ) -> PipelineStageResult:
        return PipelineStageResult(
            stage=stage,
            status="BLOCKED",
            action_taken=action_taken,
            reason=str(exc),
            recommended_action=recommended_action,
            warnings=[str(exc)],
        )

    def _overall_status(self, stages: list[PipelineStageResult]) -> str:
        effective = [stage for stage in stages if stage.status != "SKIPPED"]
        stages = effective or stages
        if all(stage.status == "PASS" for stage in stages):
            return "PASS"
        if any(stage.status in {"PASS", "PARTIAL"} for stage in stages):
            return "PARTIAL"
        return "BLOCKED"

    def _company_report_path(self, ticker: str, period_from: str, period_to: str, suffix: str) -> Path:
        return self.root / "data" / "validation" / ticker.upper() / f"{period_from}_{period_to}_{suffix}.json"

    def _relative(self, path: Path | str | None) -> str | None:
        if path is None:
            return None
        resolved = Path(path)
        if not resolved.is_absolute():
            resolved = (self.root / resolved).resolve()
        try:
            return resolved.relative_to(self.root).as_posix()
        except ValueError:
            return resolved.as_posix()

    def _load_json(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _status_summary(self, name: str, report: dict[str, Any] | None) -> dict[str, Any]:
        if not report:
            return {"status": "missing"}
        if name == "financial_ratios":
            return report.get("summary") or {}
        if name == "market_technical":
            return {"status": report.get("status"), **(report.get("summary") or {})}
        if name == "peer_analysis":
            return {
                "comparison_readiness": report.get("comparison_readiness"),
                "peer_selection_status": report.get("peer_selection_status"),
                "valuation_status": report.get("valuation_status"),
            }
        if name == "llm_payload":
            ratios = report.get("financial_ratios") or {}
            return {
                "calculated_ratios": len(ratios.get("calculated") or []),
                "blockers_count": len(report.get("blockers") or []),
            }
        if name == "fact_candidate_review":
            return {
                "candidates_count": report.get("candidates_count"),
                "eligible_count": report.get("eligible_count"),
                "needs_review_count": report.get("needs_review_count"),
                "blocked_count": report.get("blocked_count"),
            }
        if name == "fact_candidate_promotion":
            return {
                "promoted_count": report.get("promoted_count"),
                "skipped_count": report.get("skipped_count"),
                "quality_flag": (report.get("safety") or {}).get("quality_flag"),
            }
        if name == "manual_ingestion":
            return {
                "status": report.get("status"),
                "source_trust_bucket": report.get("source_trust_bucket"),
                "official_source_verified": report.get("official_source_verified"),
            }
        return {"status": "available"}
