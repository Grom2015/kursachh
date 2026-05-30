from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.services.ui.pipeline_control_center import (
    PIPELINE_JOB_STORE,
    FullPipelineRunRequest,
    PipelineControlCenter,
    PipelineRunRequest,
    SourcePipelineRunRequest,
    run_full_pipeline_job,
)

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


class DemoRunRequest(BaseModel):
    ticker: str
    period_from: str
    period_to: str
    reporting_standard: str = "IFRS"
    board: str = "TQBR"


class SourceRunRequest(BaseModel):
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


class FullRunRequest(BaseModel):
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


def _normalize_period_range(period_from: str, period_to: str) -> tuple[str, str]:
    """Accept a plain year in the UI as a full-year quarter range."""
    normalized_from = period_from.strip().upper()
    normalized_to = period_to.strip().upper()
    if normalized_from.isdigit() and len(normalized_from) == 4:
        normalized_from = f"{normalized_from}Q1"
    if normalized_to.isdigit() and len(normalized_to) == 4:
        normalized_to = f"{normalized_to}Q4"
    return normalized_from, normalized_to


@router.post("/full-run")
def run_full_pipeline(request: FullRunRequest, background_tasks: BackgroundTasks) -> dict:
    period_from, period_to = _normalize_period_range(request.period_from, request.period_to)
    job = PIPELINE_JOB_STORE.create(
        FullPipelineRunRequest(
            ticker=request.ticker,
            period_from=period_from,
            period_to=period_to,
            reporting_standard=request.reporting_standard,
            board=request.board,
            live_discovery=request.live_discovery,
            allow_document_download=request.allow_document_download,
            allow_market_live=request.allow_market_live,
            run_table_extraction=request.run_table_extraction,
            run_dataframe_fact_parser=request.run_dataframe_fact_parser,
            allow_text_fallback_semantic_gate=request.allow_text_fallback_semantic_gate,
            auto_bootstrap_identity=request.auto_bootstrap_identity,
        )
    )
    background_tasks.add_task(run_full_pipeline_job, job.job_id)
    return {"job_id": job.job_id, "status": job.status}


@router.get("/jobs/{job_id}")
def get_pipeline_job(job_id: str) -> dict:
    job = PIPELINE_JOB_STORE.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Pipeline job not found")
    return job.to_dict()


@router.get("/jobs/{job_id}/events")
def get_pipeline_job_events(job_id: str) -> dict:
    job = PIPELINE_JOB_STORE.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Pipeline job not found")
    return {
        "job_id": job.job_id,
        "status": job.status,
        "overall_status": job.overall_status,
        "current_stage": job.current_stage,
        "duration_ms": job.duration_ms,
        "events": [event.to_dict() for event in job.events],
        "result": job.result,
        "error_message": job.error_message,
    }


@router.post("/demo-run")
def run_demo_pipeline(request: DemoRunRequest, db: Session = Depends(get_db)) -> dict:
    period_from, period_to = _normalize_period_range(request.period_from, request.period_to)
    result = PipelineControlCenter(db).run_safe_report_pipeline(
        PipelineRunRequest(
            ticker=request.ticker,
            period_from=period_from,
            period_to=period_to,
            reporting_standard=request.reporting_standard,
            board=request.board,
        )
    )
    return result.to_dict()


@router.post("/draft-run")
def run_draft_candidate_pipeline(request: DemoRunRequest, db: Session = Depends(get_db)) -> dict:
    period_from, period_to = _normalize_period_range(request.period_from, request.period_to)
    result = PipelineControlCenter(db).run_draft_candidate_analysis(
        PipelineRunRequest(
            ticker=request.ticker,
            period_from=period_from,
            period_to=period_to,
            reporting_standard=request.reporting_standard,
            board=request.board,
        )
    )
    return result.to_dict()


@router.post("/review-candidates")
def review_fact_candidates(request: DemoRunRequest, db: Session = Depends(get_db)) -> dict:
    period_from, period_to = _normalize_period_range(request.period_from, request.period_to)
    result = PipelineControlCenter(db).build_fact_candidate_review(
        PipelineRunRequest(
            ticker=request.ticker,
            period_from=period_from,
            period_to=period_to,
            reporting_standard=request.reporting_standard,
            board=request.board,
        )
    )
    return result.to_dict()


class PromoteCandidatesRequest(DemoRunRequest):
    confirm: bool = False


@router.post("/promote-candidates")
def promote_fact_candidates(request: PromoteCandidatesRequest, db: Session = Depends(get_db)) -> dict:
    period_from, period_to = _normalize_period_range(request.period_from, request.period_to)
    result = PipelineControlCenter(db).promote_reviewed_candidates(
        PipelineRunRequest(
            ticker=request.ticker,
            period_from=period_from,
            period_to=period_to,
            reporting_standard=request.reporting_standard,
            board=request.board,
        ),
        confirm=request.confirm,
    )
    return result.to_dict()


@router.post("/source-run")
def run_source_pipeline(request: SourceRunRequest, db: Session = Depends(get_db)) -> dict:
    period_from, period_to = _normalize_period_range(request.period_from, request.period_to)
    result = PipelineControlCenter(db).run_source_discovery_pipeline(
        SourcePipelineRunRequest(
            ticker=request.ticker,
            period_from=period_from,
            period_to=period_to,
            reporting_standard=request.reporting_standard,
            live_discovery=request.live_discovery,
            allow_document_download=request.allow_document_download,
            run_table_extraction=request.run_table_extraction,
            run_dataframe_fact_parser=request.run_dataframe_fact_parser,
            allow_text_fallback_semantic_gate=request.allow_text_fallback_semantic_gate,
            auto_bootstrap_identity=request.auto_bootstrap_identity,
        )
    )
    return result.to_dict()


@router.get("/status")
def pipeline_status(
    ticker: str,
    period_from: str,
    period_to: str,
    reporting_standard: str = "IFRS",
    db: Session = Depends(get_db),
) -> dict:
    period_from, period_to = _normalize_period_range(period_from, period_to)
    return PipelineControlCenter(db).status(
        ticker=ticker,
        period_from=period_from,
        period_to=period_to,
        reporting_standard=reporting_standard,
    )
