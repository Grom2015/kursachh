from dataclasses import dataclass
from datetime import datetime

from app.db.models import Company


@dataclass(frozen=True)
class DiscoveredReport:
    company_ticker: str
    report_period: str
    period_from: str | None
    period_to: str | None
    reporting_standard: str
    document_type: str
    source_role: str
    source_type: str
    source_url: str
    title: str
    language: str | None
    published_at: datetime | None
    expected_file_type: str
    confidence_score: float
    notes: str | None = None

    def to_manifest_item(self) -> dict:
        return {
            "period": self.report_period,
            "period_from": self.period_from,
            "period_to": self.period_to,
            "reporting_standard": self.reporting_standard,
            "document_type": self.document_type,
            "source_role": self.source_role,
            "source_type": self.source_type,
            "source_url": self.source_url,
            "title": self.title,
            "language": self.language,
            "published_at": self.published_at,
            "expected_file_type": self.expected_file_type,
            "confidence_score": self.confidence_score,
            "notes": self.notes,
        }


class ReportSourceAdapter:
    source_type: str = "unknown"

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def supports(self, company: Company, reporting_standard: str) -> bool:
        raise NotImplementedError

    async def discover_reports(
        self,
        company: Company,
        period_from: str,
        period_to: str,
        reporting_standard: str,
    ) -> list[DiscoveredReport]:
        raise NotImplementedError
