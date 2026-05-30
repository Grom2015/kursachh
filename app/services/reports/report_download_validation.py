import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Company, ReportDocument
from app.services.reports.document_validator import (
    DocumentValidationResult,
    DocumentValidator,
)
from app.services.reports.downloader import ReportDownloader, ReportDownloadError
from app.services.reports.financial_report_discovery import DiscoveredFinancialReport


@dataclass
class ReportDownloadValidationItem:
    source_url: str
    period: str
    document_id: int | None
    status: str
    validation_status: str | None
    rejection_reason: str | None
    validation: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReportDownloadValidationReport:
    company: str
    reporting_standard: str
    period_from: str
    period_to: str
    downloaded_documents_count: int
    validated_financial_statements_count: int
    rejected_documents_count: int
    items: list[ReportDownloadValidationItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["items"] = [item.to_dict() for item in self.items]
        return payload


class ReportDownloadValidationService:
    def __init__(self, db: Session, root: Path | None = None):
        self.db = db
        self.root = root or get_settings().root_dir
        self.downloader = ReportDownloader(db)
        self.validator = DocumentValidator()

    def download_and_validate(
        self,
        company: Company,
        candidates: list[DiscoveredFinancialReport],
        period_from: str,
        period_to: str,
    ) -> ReportDownloadValidationReport:
        items: list[ReportDownloadValidationItem] = []
        warnings: list[str] = []
        for candidate in candidates:
            manifest_item = self._manifest_item(candidate)
            try:
                document = self.downloader.download(company, manifest_item)
                validation = self.validator.validate(document, expected_file_type=candidate.expected_file_type)
                accepted, reason = self._is_accepted(candidate, validation)
                if accepted:
                    document.status = "downloaded"
                    document.rejection_reason = None
                    if (
                        candidate.source_role != validation.detected_document_role
                        and validation.detected_document_role == "financial_statements"
                    ):
                        document.source_role = f"{candidate.source_role}_with_embedded_financial_statements"
                else:
                    document.status = "rejected"
                    document.rejection_reason = reason
                self.db.flush()
                items.append(
                    ReportDownloadValidationItem(
                        source_url=candidate.source_url,
                        period=candidate.period,
                        document_id=document.id,
                        status=document.status,
                        validation_status=validation.validation_status,
                        rejection_reason=document.rejection_reason,
                        validation=validation.to_dict(),
                    )
                )
            except ReportDownloadError as exc:
                warning = str(exc)
                warnings.append(warning)
                document = self._rejected_document(company, candidate, warning)
                items.append(
                    ReportDownloadValidationItem(
                        source_url=candidate.source_url,
                        period=candidate.period,
                        document_id=document.id,
                        status="rejected",
                        validation_status="failed",
                        rejection_reason=warning,
                        validation=None,
                    )
                )
        self.db.commit()
        return ReportDownloadValidationReport(
            company=company.ticker,
            reporting_standard=candidates[0].reporting_standard if candidates else "UNKNOWN",
            period_from=period_from,
            period_to=period_to,
            downloaded_documents_count=sum(1 for item in items if item.status in {"downloaded", "parsed"}),
            validated_financial_statements_count=sum(
                1
                for item in items
                if item.status in {"downloaded", "parsed"}
                and item.validation
                and item.validation.get("detected_document_role") == "financial_statements"
            ),
            rejected_documents_count=sum(1 for item in items if item.status == "rejected"),
            items=items,
            warnings=warnings,
        )

    def save_report(self, report: ReportDownloadValidationReport) -> Path:
        root = self.root / "data" / "validation" / report.company.upper()
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{report.period_from}_{report.period_to}_report_download_validation.json"
        path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _manifest_item(self, candidate: DiscoveredFinancialReport) -> dict[str, Any]:
        return {
            "period": candidate.period,
            "reporting_standard": candidate.reporting_standard,
            "document_type": candidate.document_type,
            "source_role": candidate.source_role,
            "source_type": "financial_report_discovery",
            "source_url": candidate.source_url,
            "expected_file_type": candidate.expected_file_type,
            "language": candidate.language,
        }

    def _is_accepted(self, candidate: DiscoveredFinancialReport, validation: DocumentValidationResult) -> tuple[bool, str | None]:
        if validation.validation_status != "pass":
            return False, f"document_validation_{validation.validation_status}"
        if candidate.reporting_standard.upper() != validation.detected_reporting_standard.upper():
            return False, "reporting_standard_mismatch"
        if candidate.source_role == "financial_statements":
            return validation.detected_document_role == "financial_statements", "document_role_not_financial_statements"
        if candidate.source_role in {"annual_report", "issuer_report", "press_release"}:
            markers = validation.marker_results
            embedded = (
                validation.detected_document_role == "financial_statements"
                and markers.get("company_marker")
                and markers.get("period_marker")
                and (markers.get("ifrs_marker") or markers.get("ras_marker"))
                and markers.get("consolidated_financial_statements_marker")
                and markers.get("primary_statements_marker")
                and markers.get("notes_marker")
            )
            return embedded, "embedded_financial_statements_not_proven"
        return False, "unsupported_source_role"

    def _rejected_document(self, company: Company, candidate: DiscoveredFinancialReport, reason: str) -> ReportDocument:
        document = ReportDocument(
            company_id=company.id,
            report_period=candidate.period,
            reporting_standard=candidate.reporting_standard,
            document_type=candidate.document_type,
            source_role=candidate.source_role,
            source_type="financial_report_discovery",
            source_url=candidate.source_url,
            language=candidate.language,
            status="rejected",
            rejection_reason=reason,
        )
        self.db.add(document)
        self.db.flush()
        return document
