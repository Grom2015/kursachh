from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import get_settings
from app.db.models import AnalysisJob, AnalysisResult, Company
from app.schemas.analysis import (
    AnalysisJobCreate,
    AnalysisJobCreateResponse,
    AnalysisJobOut,
    AnalysisResultOut,
    ExtendAliasRequest,
    ExtendRequest,
    HistoryItem,
    HistoryResponse,
)
from app.services.analysis.extend_service import ExtendAnalysisService, run_extend_job
from app.services.analysis.orchestrator import run_analysis_job
from app.services.periods import ensure_period_range

router = APIRouter(tags=["analysis"])


@router.post("/analysis-jobs", response_model=AnalysisJobCreateResponse)
def create_analysis_job(
    request: AnalysisJobCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
) -> AnalysisJobCreateResponse:
    try:
        ensure_period_range(request.period_from, request.period_to)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    job = AnalysisJob(
        company_query=request.company_query,
        period_from=request.period_from,
        period_to=request.period_to,
        reporting_standard=request.reporting_standard,
        include_market_data=request.include_market_data,
        include_peers=request.include_peers,
        include_news=request.include_news,
        data_mode=request.data_mode,
        status="queued",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    background_tasks.add_task(run_analysis_job, job.id)
    return AnalysisJobCreateResponse(job_id=job.id, status=job.status)


@router.post("/analyze", response_model=AnalysisJobCreateResponse)
def analyze_alias(
    request: AnalysisJobCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
) -> AnalysisJobCreateResponse:
    return create_analysis_job(request, background_tasks, db)


@router.get("/analysis-jobs/{job_id}", response_model=AnalysisJobOut)
def get_analysis_job(job_id: str, db: Session = Depends(get_db)) -> AnalysisJobOut:
    job = db.get(AnalysisJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="AnalysisJob not found")
    return AnalysisJobOut(
        job_id=job.id,
        status=job.status,
        stage=job.stage,
        progress=job.progress,
        warnings=job.warnings_json or [],
        result_id=job.result_id,
        error_message=job.error_message,
    )


@router.get("/analysis-results/{result_id}", response_model=AnalysisResultOut)
def get_analysis_result(result_id: str, db: Session = Depends(get_db)) -> AnalysisResultOut:
    result = db.get(AnalysisResult, result_id)
    if not result:
        raise HTTPException(status_code=404, detail="AnalysisResult not found")
    return AnalysisResultOut(
        id=result.id,
        company={
            "id": result.company.id,
            "ticker": result.company.ticker,
            "short_name": result.company.short_name,
            "full_name": result.company.full_name,
            "sector": result.company.sector,
            "board": result.company.board,
        },
        period_from=result.period_from,
        period_to=result.period_to,
        result=result.result_json,
        llm_payload=result.llm_payload_json,
        report_markdown=result.report_markdown,
        warnings=result.warnings_json or [],
        disclaimer=result.disclaimer,
    )


@router.post("/analysis-results/{result_id}/extend", response_model=AnalysisJobCreateResponse)
def extend_result(
    result_id: str,
    request: ExtendRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> AnalysisJobCreateResponse:
    try:
        job = ExtendAnalysisService(db).create_job(result_id, request.new_period_to)
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc).casefold() else 422
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    background_tasks.add_task(run_extend_job, job.id, result_id)
    return AnalysisJobCreateResponse(job_id=job.id, status=job.status)


@router.post("/extend", response_model=AnalysisJobCreateResponse)
def extend_alias(
    request: ExtendAliasRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> AnalysisJobCreateResponse:
    return extend_result(request.result_id, ExtendRequest(new_period_to=request.new_period_to), background_tasks, db)


@router.get("/analysis-history", response_model=HistoryResponse)
def analysis_history(
    company: str | None = None, limit: int = 20, db: Session = Depends(get_db)
) -> HistoryResponse:
    stmt = select(AnalysisResult).order_by(AnalysisResult.created_at.desc()).limit(limit)
    if company:
        company_obj = db.scalar(select(Company).where(Company.ticker == company.upper()))
        if not company_obj:
            return HistoryResponse(items=[])
        stmt = (
            select(AnalysisResult)
            .where(AnalysisResult.company_id == company_obj.id)
            .order_by(AnalysisResult.created_at.desc())
            .limit(limit)
        )
    results = db.scalars(stmt).all()
    return HistoryResponse(
        items=[
            HistoryItem(
                id=result.id,
                company=result.company.ticker,
                period_from=result.period_from,
                period_to=result.period_to,
                version=result.version,
                created_at=result.created_at,
                status="succeeded",
            )
            for result in results
        ]
    )


@router.get("/history", response_model=HistoryResponse)
def history_alias(company: str | None = None, limit: int = 20, db: Session = Depends(get_db)) -> HistoryResponse:
    return analysis_history(company, limit, db)


@router.get("/analysis-results/{result_id}/report")
def get_analysis_report(result_id: str, db: Session = Depends(get_db)) -> dict:
    result = db.get(AnalysisResult, result_id)
    if not result:
        raise HTTPException(status_code=404, detail="AnalysisResult not found")
    llm_report = (result.result_json or {}).get("llm_report") or {}
    return {
        "id": result.id,
        "company": result.company.ticker,
        "period_from": result.period_from,
        "period_to": result.period_to,
        "report_markdown": result.report_markdown,
        "fundamental_note": llm_report.get("fundamental_note"),
        "technical_note": llm_report.get("technical_note"),
        "peer_note": llm_report.get("peer_note"),
        "overall_summary": llm_report.get("overall_summary"),
        "recommendation": llm_report.get("recommendation"),
        "llm_model": llm_report.get("llm_model"),
        "token_usage": llm_report.get("token_usage"),
        "disclaimer": result.disclaimer,
    }


@router.get("/disclaimer")
def disclaimer() -> dict[str, str]:
    return {"disclaimer": get_settings().disclaimer}
