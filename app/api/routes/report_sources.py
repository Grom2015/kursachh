from fastapi import APIRouter, HTTPException

from app.services.reports.report_source_catalog import ReportSourceCatalog

router = APIRouter(prefix="/report-sources", tags=["report-sources"])


@router.get("/catalog")
def report_source_catalog() -> dict:
    return ReportSourceCatalog().all_sources()


@router.get("/{ticker}")
def report_source_for_ticker(ticker: str) -> dict:
    result = ReportSourceCatalog().source_for_ticker(ticker)
    if not result.get("found"):
        raise HTTPException(status_code=404, detail=result)
    return result
