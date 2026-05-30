import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Company
from app.services.company_registry import CompanyRegistry, normalize_query
from app.services.company_source_discovery import CompanySourceDiscoveryService
from app.services.periods import period_in_range, periods_between
from app.services.reports.report_source_catalog import EDISCLOSURE_COMPANY_IDS

REPORT_DISCOVERY_DISCLAIMER = (
    "Discovery READY means candidate report URLs were found for requested periods only. "
    "It does not mean documents are downloaded, validated, parsed, or analysis-ready."
)

SUPPORTED_DOCUMENT_TYPES = {"financial_statements", "financial_results", "annual_report", "issuer_report"}
SUPPORTED_FILE_TYPES = {"pdf", "xlsx", "html", "zip"}


@dataclass
class FinancialReportDiscoveryRequest:
    company_query: str
    period_from: str
    period_to: str
    reporting_standard: str = "IFRS"
    ticker: str | None = None
    document_types: list[str] = field(default_factory=lambda: ["financial_statements"])
    preferred_file_types: list[str] = field(default_factory=lambda: ["pdf", "xlsx", "html", "zip"])
    live: bool = False


@dataclass
class DiscoveredFinancialReport:
    company_ticker: str
    period: str
    period_type: str
    reporting_standard: str
    document_type: str
    source_role: str
    source_url: str
    source_domain: str
    source_page_url: str | None
    title: str
    language: str | None
    expected_file_type: str
    confidence_score: float
    confidence_reasons: list[str] = field(default_factory=list)
    verification_status: str = "not_checked"
    rejection_reason: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FinancialReportDiscoveryReport:
    query: str
    resolved_company: dict[str, Any] | None
    reporting_standard: str
    period_from: str
    period_to: str
    source_pages_checked: list[str]
    discovered_reports: list[DiscoveredFinancialReport]
    missing_periods: list[str]
    rejected_candidates: list[dict[str, Any]]
    warnings: list[str]
    next_actions: list[str]
    status: str
    disclaimer: str = REPORT_DISCOVERY_DISCLAIMER
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["discovered_reports"] = [item.to_dict() for item in self.discovered_reports]
        return payload


class FinancialReportDiscoveryService:
    def __init__(self, db: Session, root: Path | None = None, http_client_factory: Any | None = None):
        self.db = db
        self.root = root or get_settings().root_dir
        self.http_client_factory = http_client_factory or httpx.Client

    def discover(self, request: FinancialReportDiscoveryRequest) -> FinancialReportDiscoveryReport:
        warnings: list[str] = []
        rejected: list[dict[str, Any]] = []
        company = self._resolve_company(request)
        if not company:
            return self._empty_report(request, "AMBIGUOUS", ["Company could not be resolved uniquely."])
        candidates: list[DiscoveredFinancialReport] = []
        source_pages_checked: list[str] = []

        manifest_candidates, manifest_rejected = self._manifest_candidates(company, request)
        candidates.extend(manifest_candidates)
        rejected.extend(manifest_rejected)

        source_report = CompanySourceDiscoveryService(self.db, root=self.root).discover(
            request.company_query,
            ticker=request.ticker or company.ticker,
            live=False,
        )
        for source_candidate in source_report.recommended_candidates:
            if source_candidate.source_url and source_candidate.verification_scope == "source_page_metadata":
                source_pages_checked.append(source_candidate.source_url)
                if request.live:
                    live_candidates, live_rejected = self._source_page_candidates(source_candidate.source_url, company, request)
                    candidates.extend(live_candidates)
                    rejected.extend(live_rejected)

        if request.live:
            edisclosure_candidates, edisclosure_rejected, edisclosure_pages = self._edisclosure_files_candidates(
                company,
                request,
            )
            candidates.extend(edisclosure_candidates)
            rejected.extend(edisclosure_rejected)
            source_pages_checked.extend(edisclosure_pages)

        candidates = self._dedupe_candidates(candidates)
        periods = periods_between(request.period_from, request.period_to)
        covered = {candidate.period for candidate in candidates if candidate.source_role == "financial_statements"}
        missing_periods = [period for period in periods if period not in covered]
        status = "READY" if not missing_periods and candidates else "PARTIAL" if candidates else "NOT_FOUND"
        if source_report.auto_select_allowed is False and not company:
            status = "AMBIGUOUS"
        return FinancialReportDiscoveryReport(
            query=request.company_query,
            resolved_company=self._company_payload(company),
            reporting_standard=request.reporting_standard.upper(),
            period_from=request.period_from,
            period_to=request.period_to,
            source_pages_checked=sorted(set(source_pages_checked)),
            discovered_reports=candidates,
            missing_periods=missing_periods,
            rejected_candidates=rejected,
            warnings=sorted(set(warnings + source_report.warnings)),
            next_actions=[
                "Run report_download_validation before treating candidates as usable documents.",
                "Run statement table extraction only after documents are cached and validated.",
            ],
            status=status,
        )

    def save_report(self, report: FinancialReportDiscoveryReport) -> Path:
        ticker = (report.resolved_company or {}).get("ticker") or self._slug(report.query)
        root = self.root / "data" / "validation" / str(ticker).upper()
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{report.period_from}_{report.period_to}_financial_report_discovery.json"
        path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _resolve_company(self, request: FinancialReportDiscoveryRequest) -> Company | None:
        if request.ticker:
            company = self.db.scalar(select(Company).where(Company.ticker == request.ticker.upper()))
            if company:
                return company
        query = request.ticker or request.company_query
        return CompanyRegistry(self.db).resolve_one(query)

    def _manifest_candidates(
        self, company: Company, request: FinancialReportDiscoveryRequest
    ) -> tuple[list[DiscoveredFinancialReport], list[dict[str, Any]]]:
        path = self.root / "data" / "manifests" / f"{company.ticker.casefold()}_real_sources.yml"
        if not path.exists():
            return [], []
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        candidates: list[DiscoveredFinancialReport] = []
        rejected: list[dict[str, Any]] = []
        for item in data.get("reports", []) or []:
            if not item.get("source_url"):
                continue
            if not period_in_range(item.get("period"), request.period_from, request.period_to):
                continue
            standard = (item.get("reporting_standard") or "").upper()
            if standard != request.reporting_standard.upper():
                rejected.append({"source_url": item.get("source_url"), "rejection_reason": "reporting_standard_mismatch"})
                continue
            role = item.get("source_role") or item.get("document_type") or "unknown"
            doc_type = self._document_type(role, item.get("document_type"))
            if role != "financial_statements" and "financial_statements" in request.document_types:
                rejected.append(
                    {
                        "source_url": item.get("source_url"),
                        "period": item.get("period"),
                        "source_role": role,
                        "rejection_reason": "not_financial_statements_candidate",
                    }
                )
                continue
            expected_file_type = item.get("expected_file_type") or self._file_type(item.get("source_url"))
            if expected_file_type not in request.preferred_file_types:
                rejected.append({"source_url": item.get("source_url"), "rejection_reason": "file_type_not_preferred"})
                continue
            candidates.append(
                DiscoveredFinancialReport(
                    company_ticker=company.ticker,
                    period=item["period"],
                    period_type=period_type_for(item["period"]),
                    reporting_standard=standard,
                    document_type=doc_type,
                    source_role=role,
                    source_url=item["source_url"],
                    source_domain=self._domain(item["source_url"]),
                    source_page_url=data.get("source_page_url"),
                    title=item.get("title") or "",
                    language=item.get("language"),
                    expected_file_type=expected_file_type,
                    confidence_score=float(item.get("confidence_score", 0.9)),
                    confidence_reasons=["Candidate seeded from existing real manifest."],
                    provenance={"source": "real_manifest", "manifest_path": str(path)},
                )
            )
        return candidates, rejected

    def _source_page_candidates(
        self, source_page_url: str, company: Company, request: FinancialReportDiscoveryRequest
    ) -> tuple[list[DiscoveredFinancialReport], list[dict[str, Any]]]:
        try:
            html = self._fetch_source_page(source_page_url)
        except Exception as exc:
            return [], [{"source_page_url": source_page_url, "rejection_reason": f"source_page_fetch_failed: {exc}"}]
        candidates = []
        rejected = []
        for href, title in extract_links(html):
            url = urljoin(source_page_url, href)
            domain = self._domain(url)
            if domain not in self._trusted_domains():
                rejected.append({"source_url": url, "rejection_reason": "non_allowlisted_domain"})
                continue
            expected_file_type = self._file_type(url)
            if expected_file_type not in request.preferred_file_types:
                continue
            role, reason = classify_report_link(title or url, request.reporting_standard)
            period = detect_period(title or url)
            if not period or not period_in_range(period, request.period_from, request.period_to):
                continue
            if role != "financial_statements":
                rejected.append({"source_url": url, "title": title, "source_role": role, "rejection_reason": reason})
                continue
            candidates.append(
                DiscoveredFinancialReport(
                    company_ticker=company.ticker,
                    period=period,
                    period_type=period_type_for(period),
                    reporting_standard=request.reporting_standard.upper(),
                    document_type="financial_statement",
                    source_role="financial_statements",
                    source_url=url,
                    source_domain=domain,
                    source_page_url=source_page_url,
                    title=title,
                    language=language_for(title),
                    expected_file_type=expected_file_type,
                    confidence_score=0.75,
                    confidence_reasons=["Trusted source page link matched financial statement markers."],
                    provenance={"source": "trusted_source_page"},
                )
            )
        return candidates, rejected

    def _edisclosure_files_candidates(
        self,
        company: Company,
        request: FinancialReportDiscoveryRequest,
    ) -> tuple[list[DiscoveredFinancialReport], list[dict[str, Any]], list[str]]:
        disclosure_id = self._edisclosure_company_id(company)
        if not disclosure_id:
            return [], [], []
        candidates: list[DiscoveredFinancialReport] = []
        rejected: list[dict[str, Any]] = []
        pages_checked: list[str] = []
        for type_code in self._edisclosure_type_codes(request.reporting_standard):
            page_url = f"https://e-disclosure.ru/portal/files.aspx?id={disclosure_id}&type={type_code}"
            pages_checked.append(page_url)
            try:
                html = self._fetch_source_page(page_url)
            except Exception as exc:
                rejected.append(
                    {
                        "source_page_url": page_url,
                        "rejection_reason": f"source_page_fetch_failed: {exc}",
                    }
                )
                continue
            page_candidates, page_rejected = self._edisclosure_page_links(
                page_url,
                html,
                company,
                request,
                disclosure_id,
                type_code,
            )
            candidates.extend(page_candidates)
            rejected.extend(page_rejected)
        return candidates, rejected, pages_checked

    def _edisclosure_page_links(
        self,
        page_url: str,
        html: str,
        company: Company,
        request: FinancialReportDiscoveryRequest,
        disclosure_id: str,
        type_code: str,
    ) -> tuple[list[DiscoveredFinancialReport], list[dict[str, Any]]]:
        candidates: list[DiscoveredFinancialReport] = []
        rejected: list[dict[str, Any]] = []
        for href, title in extract_links(html):
            url = urljoin(page_url, href)
            domain = self._domain(url)
            if domain not in self._trusted_domains():
                rejected.append({"source_url": url, "rejection_reason": "non_allowlisted_domain"})
                continue
            expected_file_type = self._file_type(url)
            if expected_file_type not in request.preferred_file_types:
                continue
            period = detect_period(title or url)
            if not period or not period_in_range(period, request.period_from, request.period_to):
                continue
            role, reason = classify_report_link(title or url, request.reporting_standard)
            if not self._accept_edisclosure_candidate(type_code, role, title or url, request.reporting_standard):
                rejected.append(
                    {
                        "source_url": url,
                        "title": title,
                        "source_role": role,
                        "rejection_reason": reason or "edisclosure_required_financial_statement_markers_absent",
                    }
                )
                continue
            confidence_reasons = [
                "Trusted E-Disclosure files page matched requested reporting standard.",
                f"E-Disclosure disclosure_id={disclosure_id}, type={type_code}.",
            ]
            candidates.append(
                DiscoveredFinancialReport(
                    company_ticker=company.ticker,
                    period=period,
                    period_type=period_type_for(period),
                    reporting_standard=request.reporting_standard.upper(),
                    document_type="financial_statement",
                    source_role="financial_statements",
                    source_url=url,
                    source_domain=domain,
                    source_page_url=page_url,
                    title=title,
                    language=language_for(title),
                    expected_file_type=expected_file_type,
                    confidence_score=0.82 if type_code == "4" else 0.78,
                    confidence_reasons=confidence_reasons,
                    provenance={
                        "source": "edisclosure_files_page",
                        "disclosure_id": disclosure_id,
                        "files_page_type": type_code,
                    },
                )
            )
        return candidates, rejected

    def _fetch_source_page(self, url: str) -> str:
        with self.http_client_factory(timeout=15, follow_redirects=True) as client:
            response = client.head(url)
            if response.status_code in {403, 404, 405}:
                response = client.get(url)
            elif response.status_code < 400:
                response = client.get(url)
            response.raise_for_status()
            return response.text

    def _empty_report(
        self,
        request: FinancialReportDiscoveryRequest,
        status: str,
        warnings: list[str],
    ) -> FinancialReportDiscoveryReport:
        return FinancialReportDiscoveryReport(
            query=request.company_query,
            resolved_company=None,
            reporting_standard=request.reporting_standard.upper(),
            period_from=request.period_from,
            period_to=request.period_to,
            source_pages_checked=[],
            discovered_reports=[],
            missing_periods=periods_between(request.period_from, request.period_to),
            rejected_candidates=[],
            warnings=warnings,
            next_actions=["Resolve company identity before financial report discovery."],
            status=status,
        )

    def _company_payload(self, company: Company) -> dict[str, Any]:
        return {
            "ticker": company.ticker,
            "board": company.board,
            "short_name": company.short_name,
            "full_name": company.full_name,
        }

    def _dedupe_candidates(self, candidates: list[DiscoveredFinancialReport]) -> list[DiscoveredFinancialReport]:
        seen = set()
        rows = []
        for candidate in sorted(candidates, key=lambda item: item.confidence_score, reverse=True):
            key = (candidate.period, candidate.source_url)
            if key in seen:
                continue
            seen.add(key)
            rows.append(candidate)
        return rows

    def _trusted_domains(self) -> set[str]:
        return set(get_settings().report_source_allowed_domains) | {
            "e-disclosure.ru",
            "www.e-disclosure.ru",
            "disclosure.skrin.ru",
        }

    def _edisclosure_company_id(self, company: Company) -> str | None:
        return company.disclosure_id or EDISCLOSURE_COMPANY_IDS.get(company.ticker.upper())

    def _edisclosure_type_codes(self, reporting_standard: str) -> list[str]:
        standard = reporting_standard.upper()
        if standard == "IFRS":
            return ["4"]
        if standard == "RAS":
            return ["3"]
        return []

    def _accept_edisclosure_candidate(
        self,
        type_code: str,
        source_role: str,
        text: str,
        reporting_standard: str,
    ) -> bool:
        if source_role == "financial_statements":
            return True
        value = " ".join((text or "").casefold().split())
        if any(token in value for token in ["presentation", "презента", "press release", "пресс-релиз"]):
            return False
        if reporting_standard.upper() == "IFRS" and type_code == "4":
            return any(
                token in value
                for token in [
                    "ifrs",
                    "financial statements",
                    "financial report",
                    "консолид",
                    "финансовая отчетность",
                    "промежуточная отчетность",
                ]
            )
        if reporting_standard.upper() == "RAS" and type_code == "3":
            return any(
                token in value
                for token in [
                    "ras",
                    "accounting statements",
                    "бухгалтер",
                    "финансовая отчетность",
                    "рсбу",
                ]
            )
        return False

    def _domain(self, url: str) -> str:
        return (urlparse(url).hostname or "").casefold()

    def _file_type(self, url: str | None) -> str:
        path = (urlparse(url or "").path or "").casefold()
        for ext in SUPPORTED_FILE_TYPES:
            if path.endswith(f".{ext}"):
                return ext
        if ".pdf" in (url or "").casefold():
            return "pdf"
        return "html"

    def _document_type(self, source_role: str, document_type: str | None) -> str:
        if source_role == "financial_statements":
            return "financial_statement"
        return document_type or source_role or "unknown"

    def _slug(self, value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ_-]+", "_", normalize_query(value))
        return slug.strip("_") or "company"


def extract_links(html: str) -> list[tuple[str, str]]:
    pattern = re.compile(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
    rows = []
    for href, title in pattern.findall(html or ""):
        clean_title = re.sub(r"<[^>]+>", " ", title)
        clean_title = " ".join(clean_title.split())
        rows.append((href, clean_title))
    return rows


def classify_report_link(text: str, reporting_standard: str) -> tuple[str, str | None]:
    value = " ".join((text or "").casefold().split())
    is_ifrs = reporting_standard.upper() == "IFRS"
    if is_ifrs:
        has_standard = "ifrs" in value or "мсфо" in value
        has_statement = (
            "consolidated financial statements" in value
            or "interim condensed consolidated financial statements" in value
            or "консолидированная финансовая отчетность" in value
            or "консолидированной финансовой отчетности" in value
            or "промежуточная сокращенная консолидированная" in value
        )
        if has_standard and has_statement:
            return "financial_statements", None
        if "annual report" in value or "годовой отчет" in value or "годовой отчёт" in value:
            return "annual_report", "annual_report_not_validated_as_financial_statements"
        if "press release" in value or "пресс-релиз" in value:
            return "press_release", "press_release_not_financial_statements"
    else:
        if (
            "бухгалтерская отчетность" in value
            or "бухгалтерский баланс" in value
            or "рсбу" in value
            or "ras" in value
        ):
            return "financial_statements", None
    if "presentation" in value or "презента" in value:
        return "issuer_report", "presentation_not_financial_statements"
    return "unknown", "required_financial_statement_markers_absent"


def detect_period(text: str) -> str | None:
    value = " ".join((text or "").casefold().split())
    quarter_match = re.search(r"\b(?P<year>20\d{2})\s*[-_/]?\s*q(?P<quarter>[1-4])\b", value)
    if quarter_match:
        return f"{quarter_match.group('year')}Q{quarter_match.group('quarter')}"
    year_match = re.search(r"\b(20\d{2})\b", value)
    if not year_match:
        return None
    year = year_match.group(1)
    if "3 months" in value or "three months" in value or "3 месяца" in value:
        return f"{year}Q1"
    if "6 months" in value or "six months" in value or "6 месяцев" in value or "h1" in value:
        return f"{year}Q2"
    if "9 months" in value or "nine months" in value or "9 месяцев" in value:
        return f"{year}Q3"
    if (
        "12 months" in value
        or "year ended 31 december" in value
        or "fy " in value
        or "annual report" in value
        or "годовой отчет" in value
        or "годовой отчёт" in value
    ):
        return f"{year}Q4"
    return None


def period_type_for(period: str) -> str:
    return {"Q1": "q1", "Q2": "h1", "Q3": "nine_months", "Q4": "fy"}.get(period[-2:], "unknown")


def language_for(text: str) -> str | None:
    return "ru" if re.search(r"[а-яА-ЯёЁ]", text or "") else "en"
