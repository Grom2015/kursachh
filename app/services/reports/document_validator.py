import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.core.config import get_settings
from app.db.models import ReportDocument
from app.services.parsing.ifrs.period_detector import resolve_report_period
from app.services.parsing.text_normalization import normalize_matching_text

try:
    import pdfplumber
except Exception:  # pragma: no cover - optional runtime guard
    pdfplumber = None


IFRS_MARKERS = [
    "ifrs",
    "international financial reporting standards",
    "мсфо",
    "международным стандартам финансовой отчетности",
    "международные стандарты финансовой отчетности",
]
CONSOLIDATED_MARKERS = [
    "consolidated financial statements",
    "consolidated interim condensed financial",
    "консолидированная финансовая отчетность",
    "консолидированной финансовой отчетности",
    "обобщенная консолидированная финансовая отчетность",
    "обобщенной консолидированной финансовой отчетности",
]
ANNUAL_REPORT_MARKERS = ["annual report", "integrated report", "годовой отчет", "годовой отчёт"]
RAS_MARKERS = [
    "russian accounting standards",
    "ras",
    "рсбу",
    "бухгалтерская отчетность",
    "бухгалтерская финансовая отчетность",
]
PRESS_RELEASE_MARKERS = ["press release", "financial results"]
COMPANY_NAME_STOPWORDS = {
    "ao",
    "company",
    "group",
    "inc",
    "jsc",
    "llc",
    "pjsc",
    "public",
    "the",
    "ао",
    "зао",
    "компания",
    "общество",
    "ооо",
    "оао",
    "пао",
    "публичное",
    "акционерное",
}


@dataclass
class DocumentValidationResult:
    validation_status: str
    confidence_score: float
    detected_document_role: str
    detected_reporting_standard: str
    detected_period: str | None
    comparative_period: str | None = None
    period_source: str | None = None
    period_confidence: float | None = None
    period_warnings: list[str] = field(default_factory=list)
    marker_results: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    sha256: str | None = None
    trusted_domain: bool = False
    file_exists: bool = False
    expected_file_type_ok: bool = False
    validator_text_pages_scanned: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "validation_status": self.validation_status,
            "confidence_score": self.confidence_score,
            "detected_document_role": self.detected_document_role,
            "detected_reporting_standard": self.detected_reporting_standard,
            "detected_period": self.detected_period,
            "comparative_period": self.comparative_period,
            "period_source": self.period_source,
            "period_confidence": self.period_confidence,
            "period_warnings": self.period_warnings,
            "marker_results": self.marker_results,
            "warnings": self.warnings,
            "sha256": self.sha256,
            "trusted_domain": self.trusted_domain,
            "file_exists": self.file_exists,
            "expected_file_type_ok": self.expected_file_type_ok,
            "validator_text_pages_scanned": self.validator_text_pages_scanned,
        }


class DocumentValidator:
    def __init__(self, trusted_domains: set[str] | None = None):
        settings = get_settings()
        self.root = settings.root_dir
        self.trusted_domains = trusted_domains or set(settings.report_source_allowed_domains)

    def validate(
        self,
        document: ReportDocument,
        expected_file_type: str | None = None,
        max_text_pages: int = 3,
    ) -> DocumentValidationResult:
        warnings: list[str] = []
        path = self._storage_path(document)
        file_exists = bool(path and path.exists())
        content = path.read_bytes() if file_exists and path else b""
        sha256 = hashlib.sha256(content).hexdigest() if content else document.file_hash
        domain = (urlparse(document.source_url or "").netloc or "").casefold()
        trusted = domain in self.trusted_domains
        if not trusted:
            warnings.append("Source URL domain is not trusted by report source allowlist.")
        if not file_exists:
            warnings.append("Cached document file is missing.")
        expected_file_type = (expected_file_type or self._expected_from_document(document)).casefold()
        expected_ok = self._expected_file_type_ok(content, document, expected_file_type)
        if not expected_ok:
            warnings.append(f"Cached document does not match expected file type: {expected_file_type}.")
        text = self._extract_text(path, content, max_text_pages=max_text_pages)
        period_resolution = self._detect_period(text, document)
        marker_results = self._marker_results(text, document, period_resolution)
        role = self._detect_role(text, document)
        standard = self._detect_standard(text, document)
        score = self._score(trusted, file_exists, bool(sha256), expected_ok, marker_results, document, role, standard)
        status = "pass" if score >= 0.75 and not self._critical_marker_missing(marker_results, document) else "fail"
        if status == "fail" and score >= 0.5:
            status = "partial"
        return DocumentValidationResult(
            validation_status=status,
            confidence_score=round(score, 4),
            detected_document_role=role,
            detected_reporting_standard=standard,
            detected_period=period_resolution.effective_report_period,
            comparative_period=period_resolution.comparative_period,
            period_source=period_resolution.period_source,
            period_confidence=round(period_resolution.period_confidence, 4),
            period_warnings=list(period_resolution.period_warnings),
            marker_results=marker_results,
            warnings=warnings,
            sha256=sha256,
            trusted_domain=trusted,
            file_exists=file_exists,
            expected_file_type_ok=expected_ok,
            validator_text_pages_scanned=max_text_pages if content.startswith(b"%PDF") else None,
        )

    def _storage_path(self, document: ReportDocument) -> Path | None:
        if not document.storage_path:
            return None
        path = Path(document.storage_path)
        if path.is_absolute():
            return path
        return self.root / path

    def _expected_from_document(self, document: ReportDocument) -> str:
        if document.file_name and "." in document.file_name:
            return document.file_name.rsplit(".", 1)[-1]
        if document.source_url and ".pdf" in document.source_url.casefold():
            return "pdf"
        return "unknown"

    def _expected_file_type_ok(self, content: bytes, document: ReportDocument, expected: str) -> bool:
        if expected in {"unknown", ""}:
            return True
        if expected == "pdf":
            return content.startswith(b"%PDF") or (document.file_name or "").casefold().endswith(".pdf")
        if expected in {"xlsx", "xls"}:
            return (document.file_name or "").casefold().endswith((".xlsx", ".xls"))
        return True

    def _extract_text(self, path: Path | None, content: bytes, max_text_pages: int = 3) -> str:
        if not content:
            return ""
        if content.startswith(b"%PDF") and path and pdfplumber:
            try:
                with pdfplumber.open(path) as pdf:
                    page_limit = max(1, max_text_pages)
                    return normalize_matching_text("\n".join((page.extract_text() or "") for page in pdf.pages[:page_limit]))
            except Exception:
                return normalize_matching_text(content[:10000].decode("utf-8", errors="ignore"))
        return normalize_matching_text(content[:10000].decode("utf-8", errors="ignore"))

    def _marker_results(self, text: str, document: ReportDocument, period_resolution) -> dict[str, bool]:
        return {
            "company_marker": self._company_marker_present(text, document),
            "ifrs_marker": any(marker in text for marker in IFRS_MARKERS),
            "consolidated_financial_statements_marker": any(marker in text for marker in CONSOLIDATED_MARKERS),
            "annual_report_marker": any(marker in text for marker in ANNUAL_REPORT_MARKERS),
            "ras_marker": any(marker in text for marker in RAS_MARKERS),
            "press_release_marker": any(marker in text for marker in PRESS_RELEASE_MARKERS),
            "primary_statements_marker": any(
                marker in text
                for marker in [
                    "statement of financial position",
                    "statement of profit or loss",
                    "statement of cash flows",
                    "отчет о финансовом положении",
                    "отчёт о финансовом положении",
                    "отчет о прибылях и убытках",
                    "отчёт о прибылях и убытках",
                    "бухгалтерский баланс",
                    "отчет о финансовых результатах",
                    "отчёт о финансовых результатах",
                    "отчет о движении денежных средств",
                    "отчёт о движении денежных средств",
                ]
            ),
            "notes_marker": "notes to" in text or "примечания" in text,
            "period_marker": period_resolution.period_source != "upload_default"
            and bool(period_resolution.effective_report_period),
            "auditor_marker": any(
                marker in text
                for marker in [
                    "independent auditor",
                    "auditor's report",
                    "auditor report",
                    "аудиторское заключение",
                    "независимого аудитора",
                ]
            ),
        }

    def _company_marker_present(self, text: str, document: ReportDocument) -> bool:
        company = document.company
        values = [
            document.company.ticker if company else "",
            company.short_name if company else "",
            company.full_name if company else "",
            *(company.aliases_json or [] if company else []),
        ]
        normalized_text = self._normalize_company_text(text)
        text_tokens = set(normalized_text.split())
        for value in values:
            normalized_value = self._normalize_company_text(value)
            if not normalized_value:
                continue
            if len(normalized_value) >= 3 and normalized_value in normalized_text:
                return True
            tokens = self._company_name_tokens(normalized_value)
            if len(tokens) >= 2 and tokens.issubset(text_tokens):
                return True
            if len(tokens) == 1:
                token = next(iter(tokens))
                if len(token) >= 5 and token in text_tokens:
                    return True
        return False

    def _normalize_company_text(self, value: str | None) -> str:
        text = normalize_matching_text(value)
        text = re.sub(r"[^0-9a-zа-я]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _company_name_tokens(self, normalized_value: str) -> set[str]:
        return {
            token
            for token in normalized_value.split()
            if len(token) >= 3 and token not in COMPANY_NAME_STOPWORDS
        }

    def _detect_role(self, text: str, document: ReportDocument) -> str:
        if any(marker in text for marker in CONSOLIDATED_MARKERS):
            return "financial_statements"
        if (
            "бухгалтерская отчетность" in text
            or "бухгалтерский баланс" in text
            or "отчет о финансовых результатах" in text
            or "отчёт о финансовых результатах" in text
            or "отчет о движении денежных средств" in text
            or "отчёт о движении денежных средств" in text
        ):
            return "financial_statements"
        if any(marker in text for marker in ANNUAL_REPORT_MARKERS):
            return "annual_report"
        if any(marker in text for marker in PRESS_RELEASE_MARKERS):
            return "press_release"
        return document.source_role or "other"

    def _detect_standard(self, text: str, document: ReportDocument) -> str:
        if any(marker in text for marker in IFRS_MARKERS):
            return "IFRS"
        if any(marker in text for marker in RAS_MARKERS):
            return "RAS"
        return document.reporting_standard or "UNKNOWN"

    def _detect_period(self, text: str, document: ReportDocument):
        return resolve_report_period(
            title_text=text,
            headers=[],
            report_period=document.report_period,
            filename_hint=document.file_name,
        )

    def _score(
        self,
        trusted: bool,
        file_exists: bool,
        has_sha: bool,
        expected_ok: bool,
        markers: dict[str, bool],
        document: ReportDocument,
        role: str,
        standard: str,
    ) -> float:
        score = 0.0
        score += 0.15 if trusted else 0.0
        score += 0.15 if file_exists else 0.0
        score += 0.10 if has_sha else 0.0
        score += 0.10 if expected_ok else 0.0
        score += 0.15 if markers["company_marker"] else 0.0
        score += 0.15 if standard == document.reporting_standard else 0.0
        if document.source_role == "financial_statements":
            score += 0.15 if role == "financial_statements" else 0.0
        elif document.source_role == "annual_report":
            score += 0.15 if role == "annual_report" else 0.0
        else:
            score += 0.10 if role == document.source_role else 0.0
        score += 0.05 if markers["period_marker"] else 0.0
        return score

    def _critical_marker_missing(self, markers: dict[str, bool], document: ReportDocument) -> bool:
        if document.source_role == "financial_statements":
            if document.reporting_standard == "RAS":
                return not (markers["ras_marker"] and markers["primary_statements_marker"])
            return not (markers["ifrs_marker"] and markers["consolidated_financial_statements_marker"])
        if document.source_role == "annual_report":
            return not markers["annual_report_marker"]
        if document.source_role == "press_release":
            return not markers["press_release_marker"]
        return False
