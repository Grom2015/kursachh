from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx

from app.core.config import get_settings
from app.db.models import Company, ReportDocument
from app.services.periods import period_in_range
from app.services.reports.document_validator import DocumentValidator
from app.services.reports.financial_report_discovery import classify_report_link, detect_period, extract_links

SUPPORTED_DOWNLOAD_TYPES = {"pdf", "xlsx", "html", "zip"}
DISCLOSURE_DOMAINS = {"e-disclosure.ru", "www.e-disclosure.ru", "disclosure.skrin.ru"}


@dataclass
class CatalogDownloadCheckRequest:
    period_from: str
    period_to: str
    reporting_standard: str = "IFRS"
    limit: int | None = None
    download: bool = True
    max_downloads_per_company: int = 4


@dataclass
class CatalogCandidate:
    period: str
    source_url: str
    source_domain: str
    source_page_url: str
    title: str
    expected_file_type: str
    source_role: str
    classification_reason: str | None = None


@dataclass
class RejectedCatalogCandidate:
    source_url: str
    title: str
    expected_file_type: str
    rejection_reason: str


@dataclass
class CatalogDownloadedDocument:
    period: str
    source_url: str
    storage_path: str
    sha256: str
    file_size_bytes: int
    validation_status: str
    detected_document_role: str
    detected_reporting_standard: str
    marker_results: dict[str, bool]
    validation_warnings: list[str]


@dataclass
class CatalogCompanyCheck:
    rank: int
    ticker: str
    company_name: str
    sector: str
    reports_url: str
    page_status: str
    report_candidates_found: int
    documents_downloaded: int
    documents_validated: int
    status: str
    reason: str | None = None
    source_statuses: dict[str, bool] = field(default_factory=dict)
    acquisition_blockers: list[str] = field(default_factory=list)
    preferred_acquisition_method: str | None = None
    candidates: list[CatalogCandidate] = field(default_factory=list)
    downloaded_documents: list[CatalogDownloadedDocument] = field(default_factory=list)
    rejected_candidate_samples: list[RejectedCatalogCandidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class MoexTop50CatalogChecker:
    def __init__(
        self,
        root: Path | None = None,
        http_client_factory: Any | None = None,
    ):
        self.root = (root or get_settings().root_dir).resolve()
        self.http_client_factory = http_client_factory or httpx.Client
        self.catalog_path = self.root / "data" / "reference" / "moex_top50_report_sources.csv"
        self.raw_root = self.root / "data" / "raw" / "catalog_sources"

    def run(self, request: CatalogDownloadCheckRequest) -> dict[str, Any]:
        rows = self._catalog_rows()
        if request.limit is not None:
            rows = rows[: max(0, request.limit)]
        company_checks = [self._check_company(row, request) for row in rows]
        summary = self._summary(company_checks)
        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "catalog_path": self._relative(self.catalog_path),
            "period_from": request.period_from,
            "period_to": request.period_to,
            "reporting_standard": request.reporting_standard.upper(),
            "download_enabled": request.download,
            "companies_checked": len(company_checks),
            "summary": summary,
            "company_checks": [self._company_payload(item) for item in company_checks],
            "safety": {
                "facts_persisted": False,
                "metric_engine_invoked": False,
                "valuation_invoked": False,
                "llm_invoked": False,
                "manifests_mutated": False,
                "downloads_storage": self._relative(self.raw_root),
            },
            "limitations": [
                "This checks public issuer report pages and downloadable candidates only.",
                "A successful download/validation is not the same as full statement fact extraction.",
                "Sites that require JavaScript, authorization, or non-standard archives may need manual/provider integration.",
            ],
        }
        return report

    def save_report(self, report: dict[str, Any]) -> Path:
        output_dir = self.root / "data" / "validation" / "report_source_catalog"
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "moex_top50_download_check.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _catalog_rows(self) -> list[dict[str, str]]:
        with self.catalog_path.open("r", encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    def _check_company(self, row: dict[str, str], request: CatalogDownloadCheckRequest) -> CatalogCompanyCheck:
        warnings: list[str] = []
        candidates: list[CatalogCandidate] = []
        rejected: list[RejectedCatalogCandidate] = []
        downloaded: list[CatalogDownloadedDocument] = []
        page_status = "not_checked"
        reason: str | None = None
        try:
            html = self._fetch_page(row["reports_url"])
            page_status = "PASS"
            candidates, rejected = self._candidates_from_page(row["reports_url"], html, request)
            if request.download:
                for candidate in candidates[: request.max_downloads_per_company]:
                    try:
                        downloaded.append(self._download_and_validate(row, candidate, request.reporting_standard))
                    except Exception as exc:
                        warnings.append(f"{candidate.source_url}: {exc}")
        except Exception as exc:
            page_status = "BLOCKED"
            reason = f"reports_page_fetch_failed: {exc}"
            warnings.append(reason)
        validated = sum(1 for item in downloaded if item.validation_status == "pass")
        status = self._status(page_status, candidates, downloaded, validated, request)
        if not reason:
            reason = self._reason(page_status, candidates, downloaded, validated, request)
        source_statuses = self._source_statuses(page_status, candidates, downloaded, validated)
        acquisition_blockers = self._acquisition_blockers(page_status, reason, candidates, downloaded, validated)
        return CatalogCompanyCheck(
            rank=int(row["rank"]),
            ticker=row["ticker"],
            company_name=row["company_name"],
            sector=row["sector"],
            reports_url=row["reports_url"],
            page_status=page_status,
            report_candidates_found=len(candidates),
            documents_downloaded=len(downloaded),
            documents_validated=validated,
            status=status,
            reason=reason,
            source_statuses=source_statuses,
            acquisition_blockers=acquisition_blockers,
            preferred_acquisition_method=row.get("preferred_acquisition_method") or self._preferred_method(acquisition_blockers),
            candidates=candidates,
            downloaded_documents=downloaded,
            rejected_candidate_samples=rejected[:10],
            warnings=warnings,
        )

    def _fetch_page(self, url: str) -> str:
        with self.http_client_factory(timeout=20, follow_redirects=True) as client:
            response = client.get(url, headers={"User-Agent": "financial-report-catalog-checker/1.0"})
            response.raise_for_status()
            return response.text

    def _candidates_from_page(
        self,
        source_page_url: str,
        html: str,
        request: CatalogDownloadCheckRequest,
    ) -> tuple[list[CatalogCandidate], list[RejectedCatalogCandidate]]:
        candidates: list[CatalogCandidate] = []
        rejected: list[RejectedCatalogCandidate] = []
        page_domain = self._domain(source_page_url)
        seen: set[str] = set()
        for href, title in self._extract_links_from_html_and_scripts(html):
            url = urljoin(source_page_url, href)
            if url in seen:
                continue
            seen.add(url)
            domain = self._domain(url)
            if domain != page_domain and domain not in DISCLOSURE_DOMAINS:
                rejected.append(self._rejected(url, title, "unknown", "non_issuer_or_disclosure_domain"))
                continue
            file_type = self._file_type(url)
            if file_type not in SUPPORTED_DOWNLOAD_TYPES:
                rejected.append(self._rejected(url, title, file_type, "unsupported_or_non_downloadable_file_type"))
                continue
            text = f"{title} {url}"
            role, reason = self._classify_catalog_report_link(text, request.reporting_standard)
            if role not in {"financial_statements", "annual_report"}:
                rejected.append(self._rejected(url, title, file_type, reason or "not_financial_report_candidate"))
                continue
            period = self._detect_catalog_period(text, role)
            if not period or not period_in_range(period, request.period_from, request.period_to):
                rejected.append(self._rejected(url, title, file_type, "period_not_detected_or_out_of_requested_range"))
                continue
            candidates.append(
                CatalogCandidate(
                    period=period,
                    source_url=url,
                    source_domain=domain,
                    source_page_url=source_page_url,
                    title=title or Path(urlparse(url).path).name,
                    expected_file_type=file_type,
                    source_role=role,
                    classification_reason=reason,
                )
            )
        return candidates, rejected

    def _download_and_validate(
        self,
        row: dict[str, str],
        candidate: CatalogCandidate,
        reporting_standard: str,
    ) -> CatalogDownloadedDocument:
        with self.http_client_factory(timeout=45, follow_redirects=True) as client:
            response = client.get(candidate.source_url, headers={"User-Agent": "financial-report-catalog-checker/1.0"})
            response.raise_for_status()
            content = response.content
        max_bytes = get_settings().max_report_download_mb * 1024 * 1024
        if len(content) > max_bytes:
            raise ValueError("download_exceeds_max_report_download_mb")
        sha256 = hashlib.sha256(content).hexdigest()
        filename = self._safe_filename(candidate.source_url, candidate.expected_file_type)
        storage_dir = (self.raw_root / row["ticker"].upper() / reporting_standard.upper() / candidate.period).resolve()
        if not storage_dir.is_relative_to(self.raw_root.resolve()):
            raise ValueError("storage_path_escapes_catalog_download_root")
        storage_dir.mkdir(parents=True, exist_ok=True)
        path = storage_dir / f"{sha256}_{filename}"
        if not path.exists():
            path.write_bytes(content)
        company = Company(
            ticker=row["ticker"].upper(),
            short_name=row["ticker"].upper(),
            full_name=row["company_name"],
            aliases_json=[row["ticker"].upper(), row["company_name"]],
        )
        document = ReportDocument(
            company=company,
            report_period=candidate.period,
            reporting_standard=reporting_standard.upper(),
            document_type=candidate.source_role,
            source_role=candidate.source_role,
            source_type="catalog_official_issuer_page",
            source_url=candidate.source_url,
            storage_path=str(path),
            file_name=path.name,
            file_hash=sha256,
            status="downloaded",
        )
        validation = DocumentValidator(trusted_domains={candidate.source_domain}).validate(
            document,
            expected_file_type=candidate.expected_file_type,
            max_text_pages=12,
        )
        return CatalogDownloadedDocument(
            period=candidate.period,
            source_url=candidate.source_url,
            storage_path=self._relative(path),
            sha256=sha256,
            file_size_bytes=len(content),
            validation_status=validation.validation_status,
            detected_document_role=validation.detected_document_role,
            detected_reporting_standard=validation.detected_reporting_standard,
            marker_results=validation.marker_results,
            validation_warnings=validation.warnings,
        )

    def _status(
        self,
        page_status: str,
        candidates: list[CatalogCandidate],
        downloaded: list[CatalogDownloadedDocument],
        validated: int,
        request: CatalogDownloadCheckRequest,
    ) -> str:
        if page_status == "BLOCKED":
            return "BLOCKED"
        if not candidates:
            return "BLOCKED"
        if not request.download:
            return "PASS" if any(candidate.source_role == "financial_statements" for candidate in candidates) else "PARTIAL"
        if validated:
            return "PASS" if any(candidate.source_role == "financial_statements" for candidate in candidates) else "PARTIAL"
        if downloaded:
            return "PARTIAL"
        return "BLOCKED"

    def _reason(
        self,
        page_status: str,
        candidates: list[CatalogCandidate],
        downloaded: list[CatalogDownloadedDocument],
        validated: int,
        request: CatalogDownloadCheckRequest,
    ) -> str | None:
        if page_status == "BLOCKED":
            return "reports_page_fetch_failed"
        if not candidates:
            return "no_downloadable_financial_statement_links_found"
        if not request.download:
            return None
        if not downloaded:
            return "candidate_download_failed"
        if not validated:
            return "downloaded_documents_failed_validation"
        if not any(candidate.source_role == "financial_statements" for candidate in candidates):
            return "annual_report_validated_financial_statements_not_directly_found"
        return None

    def _summary(self, checks: list[CatalogCompanyCheck]) -> dict[str, Any]:
        return {
            "pass_count": sum(1 for item in checks if item.status == "PASS"),
            "partial_count": sum(1 for item in checks if item.status == "PARTIAL"),
            "blocked_count": sum(1 for item in checks if item.status == "BLOCKED"),
            "pages_reachable_count": sum(1 for item in checks if item.page_status == "PASS"),
            "report_candidates_found_count": sum(item.report_candidates_found for item in checks),
            "direct_report_links_found_count": sum(
                1 for item in checks for candidate in item.candidates if candidate.source_role == "financial_statements"
            ),
            "downloadable_documents_found_count": sum(item.report_candidates_found for item in checks),
            "documents_downloaded_count": sum(item.documents_downloaded for item in checks),
            "documents_validated_count": sum(item.documents_validated for item in checks),
            "top_blockers": self._top_blockers(checks),
            "source_status_counts": self._source_status_counts(checks),
        }

    def _top_blockers(self, checks: list[CatalogCompanyCheck]) -> list[dict[str, Any]]:
        buckets: dict[str, list[str]] = {}
        for item in checks:
            if item.status == "PASS":
                continue
            reason = item.reason or "unknown"
            buckets.setdefault(reason, []).append(item.ticker)
        return [
            {"reason": reason, "count": len(tickers), "tickers_sample": tickers[:10]}
            for reason, tickers in sorted(buckets.items(), key=lambda pair: len(pair[1]), reverse=True)
        ]

    def _company_payload(self, item: CatalogCompanyCheck) -> dict[str, Any]:
        payload = asdict(item)
        payload["candidates"] = [asdict(candidate) for candidate in item.candidates]
        payload["downloaded_documents"] = [asdict(document) for document in item.downloaded_documents]
        payload["rejected_candidate_samples"] = [asdict(candidate) for candidate in item.rejected_candidate_samples]
        return payload

    def _extract_links_from_html_and_scripts(self, html: str) -> list[tuple[str, str]]:
        links = list(extract_links(html))
        text = (html or "").replace("\\/", "/").replace("\\u002F", "/")
        url_pattern = re.compile(
            r"(https?://[^\"'\s<>]+|/[A-Za-z0-9_./%?=&,:;+-]+?\.(?:pdf|xlsx|zip|html?)(?:\?[^\"'\s<>]*)?)",
            re.IGNORECASE,
        )
        for match in url_pattern.findall(text):
            url = match
            title = unquote(Path(urlparse(url).path).name or url)
            links.append((url, title))
        return links

    def _classify_catalog_report_link(self, text: str, reporting_standard: str) -> tuple[str, str | None]:
        role, reason = classify_report_link(text, reporting_standard)
        if role != "unknown":
            return role, reason
        value = " ".join(re.sub(r"[-_]+", " ", (text or "").casefold()).split())
        is_ifrs = reporting_standard.upper() == "IFRS"
        if is_ifrs and self._has_ifrs_marker(value) and self._has_financial_statement_marker(value):
            return "financial_statements", "catalog_financial_statement_markers"
        if self._has_annual_report_marker(value):
            return "annual_report", "annual_report_candidate_requires_validation"
        if "financial results" in value or "results and reports" in value or "отчет" in value:
            return "issuer_report", "issuer_report_or_results_not_financial_statements"
        return "unknown", "required_financial_statement_markers_absent"

    def _has_ifrs_marker(self, value: str) -> bool:
        return any(marker in value for marker in ("ifrs", "мсфо", "international financial reporting standards"))

    def _has_financial_statement_marker(self, value: str) -> bool:
        return any(
            marker in value
            for marker in (
                "consolidated financial statements",
                "interim condensed consolidated financial statements",
                "financial statements",
                "консолидированная финансовая отчетность",
                "консолидированной финансовой отчетности",
                "консолидированная отчетность",
                "финансовая отчетность",
            )
        )

    def _has_annual_report_marker(self, value: str) -> bool:
        return any(marker in value for marker in ("annual report", "integrated report", "годовой отчет", "годовой отчёт"))

    def _detect_catalog_period(self, text: str, source_role: str) -> str | None:
        period = detect_period(text)
        if period:
            return period
        value = " ".join((text or "").casefold().split())
        year_match = re.search(r"\b(20\d{2})\b", value)
        if not year_match:
            return None
        year = year_match.group(1)
        if source_role == "annual_report" or "fy" in value or "год" in value:
            return f"{year}Q4"
        if source_role == "financial_statements" and ("ifrs" in value or "мсфо" in value):
            return f"{year}Q4"
        return None

    def _rejected(
        self,
        url: str,
        title: str,
        expected_file_type: str,
        rejection_reason: str,
    ) -> RejectedCatalogCandidate:
        return RejectedCatalogCandidate(
            source_url=url,
            title=title,
            expected_file_type=expected_file_type,
            rejection_reason=rejection_reason,
        )

    def _source_statuses(
        self,
        page_status: str,
        candidates: list[CatalogCandidate],
        downloaded: list[CatalogDownloadedDocument],
        validated: int,
    ) -> dict[str, bool]:
        return {
            "source_page_reachable": page_status == "PASS",
            "direct_report_links_found": any(candidate.source_role == "financial_statements" for candidate in candidates),
            "downloadable_documents_found": bool(candidates),
            "documents_downloaded": bool(downloaded),
            "documents_validated": validated > 0,
            "requires_js_adapter": page_status == "PASS" and not candidates,
            "requires_disclosure_provider": page_status == "BLOCKED",
            "requires_manual_upload": validated == 0,
        }

    def _source_status_counts(self, checks: list[CatalogCompanyCheck]) -> dict[str, int]:
        keys = [
            "source_page_reachable",
            "direct_report_links_found",
            "downloadable_documents_found",
            "documents_downloaded",
            "documents_validated",
            "requires_js_adapter",
            "requires_disclosure_provider",
            "requires_manual_upload",
        ]
        return {key: sum(1 for item in checks if item.source_statuses.get(key)) for key in keys}

    def _acquisition_blockers(
        self,
        page_status: str,
        reason: str | None,
        candidates: list[CatalogCandidate],
        downloaded: list[CatalogDownloadedDocument],
        validated: int,
    ) -> list[str]:
        blockers: list[str] = []
        reason_text = reason or ""
        if page_status == "BLOCKED":
            if "401" in reason_text or "403" in reason_text:
                blockers.append("source_page_access_blocked")
            elif "certificate" in reason_text or "SSL" in reason_text:
                blockers.append("source_page_tls_blocked")
            elif "404" in reason_text:
                blockers.append("source_page_url_stale_or_not_found")
            elif "timed out" in reason_text:
                blockers.append("source_page_timeout")
            else:
                blockers.append("source_page_fetch_failed")
            blockers.append("requires_disclosure_provider")
        elif not candidates:
            blockers.append("requires_js_adapter")
            blockers.append("requires_disclosure_provider")
        elif not downloaded:
            blockers.append("candidate_download_failed")
        elif not validated:
            blockers.append("downloaded_documents_failed_validation")
        if validated == 0:
            blockers.append("requires_manual_upload")
        return sorted(set(blockers))

    def _preferred_method(self, blockers: list[str]) -> str:
        if "requires_js_adapter" in blockers:
            return "issuer_specific_js_or_embedded_json_adapter"
        if "requires_disclosure_provider" in blockers:
            return "edisclosure_or_skrin_provider_fallback"
        if "requires_manual_upload" in blockers:
            return "manual_upload_fallback"
        return "official_issuer_direct_download"

    def _file_type(self, url: str) -> str:
        path = (urlparse(url).path or "").casefold()
        if path.endswith(".pdf") or ".pdf" in url.casefold():
            return "pdf"
        if path.endswith(".xlsx"):
            return "xlsx"
        if path.endswith(".zip"):
            return "zip"
        if path.endswith(".html") or path.endswith(".htm"):
            return "html"
        return "unknown"

    def _safe_filename(self, url: str, expected_file_type: str) -> str:
        name = unquote(Path(urlparse(url).path).name)
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
        if not name:
            name = f"report.{expected_file_type}"
        if expected_file_type in SUPPORTED_DOWNLOAD_TYPES and not name.casefold().endswith(f".{expected_file_type}"):
            name = f"{name}.{expected_file_type}"
        if "/" in name or "\\" in name or ".." in name:
            raise ValueError("unsafe_report_filename")
        return name

    def _domain(self, url: str) -> str:
        return (urlparse(url).hostname or "").casefold()

    def _relative(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return str(path)
