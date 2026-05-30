import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def uuid_str() -> str:
    return str(uuid.uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class Company(Base, TimestampMixin):
    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("ticker", "board", name="uq_company_ticker_board"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    isin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    board: Mapped[str] = mapped_column(String(32), default="TQBR")
    short_name: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(512))
    inn: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(128), nullable=True)
    subsector: Mapped[str | None] = mapped_column(String(128), nullable=True)
    aliases_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    ir_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    disclosure_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    identity_status: Mapped[str] = mapped_column(String(64), default="resolved_registry")
    verification_scope: Mapped[str] = mapped_column(String(64), default="registry")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ReportDocument(Base, TimestampMixin):
    __tablename__ = "report_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    report_period: Mapped[str] = mapped_column(String(16), index=True)
    period_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    reporting_standard: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    document_type: Mapped[str] = mapped_column(String(64), default="other")
    source_role: Mapped[str] = mapped_column(String(64), default="other")
    source_type: Mapped[str] = mapped_column(String(64), default="other")
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    storage_path: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_warnings_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    trust_level: Mapped[str] = mapped_column(String(64), default="unknown")
    manual_upload_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_trust_bucket: Mapped[str] = mapped_column(String(64), default="unknown")
    official_source_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    source_package_ready_contribution: Mapped[bool] = mapped_column(Boolean, default=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="discovered")
    company: Mapped[Company] = relationship()


class StatementFact(Base):
    __tablename__ = "statement_facts"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "report_document_id",
            "period",
            "metric_code",
            "reporting_standard",
            name="uq_statement_fact_source_metric",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    report_document_id: Mapped[int | None] = mapped_column(ForeignKey("report_documents.id"), nullable=True)
    period: Mapped[str] = mapped_column(String(16), index=True)
    reporting_standard: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    statement_type: Mapped[str] = mapped_column(String(64), default="other")
    metric_code: Mapped[str] = mapped_column(String(128), index=True)
    metric_name_original: Mapped[str | None] = mapped_column(String(512), nullable=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    unit_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    period_type: Mapped[str] = mapped_column(String(32), default="quarter")
    source_location: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    quality_flag: Mapped[str] = mapped_column(String(64), default="exact")
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MetricValue(Base):
    __tablename__ = "metric_values"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    period: Mapped[str] = mapped_column(String(16), index=True)
    metric_code: Mapped[str] = mapped_column(String(128), index=True)
    metric_name: Mapped[str] = mapped_column(String(255))
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    display_value: Mapped[str | None] = mapped_column(String(128), nullable=True)
    formula: Mapped[str] = mapped_column(String(512))
    inputs_json: Mapped[dict] = mapped_column(JSON, default=dict)
    quality_flag: Mapped[str] = mapped_column(String(64), default="exact")
    warnings_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MarketCandle(Base):
    __tablename__ = "market_candles"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "trade_date",
            "source",
            name="uq_market_candle_company_date_source",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    board: Mapped[str] = mapped_column(String(32), default="TQBR")
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    open: Mapped[float | None] = mapped_column(Float, nullable=True)
    high: Mapped[float | None] = mapped_column(Float, nullable=True)
    low: Mapped[float | None] = mapped_column(Float, nullable=True)
    close: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="moex_iss")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PeerGroup(Base):
    __tablename__ = "peer_groups"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "peer_company_id",
            name="uq_peer_group_company_peer",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    peer_company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    sector: Mapped[str | None] = mapped_column(String(128), nullable=True)
    subsector: Mapped[str | None] = mapped_column(String(128), nullable=True)
    peer_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inclusion_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    peer_company: Mapped[Company] = relationship(foreign_keys=[peer_company_id])


class AnalysisJob(Base, TimestampMixin):
    __tablename__ = "analysis_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    company_query: Mapped[str] = mapped_column(String(255))
    period_from: Mapped[str] = mapped_column(String(16))
    period_to: Mapped[str] = mapped_column(String(16))
    reporting_standard: Mapped[str] = mapped_column(String(32), default="IFRS")
    include_market_data: Mapped[bool] = mapped_column(Boolean, default=True)
    include_peers: Mapped[bool] = mapped_column(Boolean, default=True)
    include_news: Mapped[bool] = mapped_column(Boolean, default=False)
    data_mode: Mapped[str] = mapped_column(String(16), default="fixture")
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    stage: Mapped[str | None] = mapped_column(String(128), nullable=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    result_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class AnalysisResult(Base):
    __tablename__ = "analysis_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    job_id: Mapped[str] = mapped_column(ForeignKey("analysis_jobs.id"), index=True)
    parent_result_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    period_from: Mapped[str] = mapped_column(String(16))
    period_to: Mapped[str] = mapped_column(String(16))
    reporting_standard: Mapped[str] = mapped_column(String(32), default="IFRS")
    data_snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    llm_payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    report_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    disclaimer: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    company: Mapped[Company] = relationship()
