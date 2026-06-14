from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AnalysisJobCreate(BaseModel):
    company_query: str
    period_from: str
    period_to: str
    reporting_standard: str = "IFRS"
    include_market_data: bool = True
    include_peers: bool = True
    include_news: bool = False
    output_language: str = "ru"
    data_mode: str = Field(default="fixture", pattern="^(fixture|real|auto)$")


class AnalysisJobCreateResponse(BaseModel):
    job_id: str
    status: str


class AnalysisJobOut(BaseModel):
    job_id: str
    status: str
    stage: str | None
    progress: float
    warnings: list[str]
    result_id: str | None
    error_message: str | None = None


class AnalysisResultOut(BaseModel):
    id: str
    company: dict[str, Any]
    period_from: str
    period_to: str
    result: dict[str, Any]
    llm_payload: dict[str, Any]
    report_markdown: str | None = None
    warnings: list[str]
    disclaimer: str


class ExtendRequest(BaseModel):
    new_period_to: str
    force_refetch: bool = False


class ExtendAliasRequest(BaseModel):
    result_id: str
    new_period_to: str
    force_refetch: bool = False


class HistoryItem(BaseModel):
    id: str
    company: str
    period_from: str
    period_to: str
    version: int
    created_at: datetime | None
    status: str


class HistoryResponse(BaseModel):
    items: list[HistoryItem]
