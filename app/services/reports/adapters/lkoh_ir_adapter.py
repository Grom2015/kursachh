import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from app.core.config import get_settings
from app.db.models import Company
from app.services.periods import periods_between
from app.services.reports.real_manifest import RealSourceManifest
from app.services.reports.source_adapters import DiscoveredReport, ReportSourceAdapter


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._href:
            self.links.append((self._href, " ".join(self._text).strip()))
            self._href = None
            self._text = []


class LKOHIRAdapter(ReportSourceAdapter):
    source_type = "issuer_ir"

    def supports(self, company: Company, reporting_standard: str) -> bool:
        return company.ticker.upper() == "LKOH" and reporting_standard.upper() == "IFRS"

    async def discover_reports(
        self,
        company: Company,
        period_from: str,
        period_to: str,
        reporting_standard: str,
    ) -> list[DiscoveredReport]:
        if not self.supports(company, reporting_standard):
            return []
        reports = await self._discover_from_ir_page(company, period_from, period_to, reporting_standard)
        if reports:
            return reports
        manifest = RealSourceManifest()
        reports = manifest.load_for_company(company.ticker, period_from, period_to, reporting_standard)
        self.warnings.extend(manifest.warnings)
        return reports

    async def _discover_from_ir_page(
        self, company: Company, period_from: str, period_to: str, reporting_standard: str
    ) -> list[DiscoveredReport]:
        settings = get_settings()
        page_url = company.ir_url or settings.lkoh_ir_reports_url
        if not page_url:
            self.warnings.append("LKOH IR URL is not configured; real manifest fallback will be used")
            return []
        if not self._is_allowed_url(page_url):
            self.warnings.append(f"LKOH IR URL is outside allowlist: {page_url}")
            return []
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(page_url)
                response.raise_for_status()
        except Exception as exc:
            self.warnings.append(f"LKOH IR discovery failed: {exc}")
            return []

        parser = _LinkParser()
        parser.feed(response.text)
        discovered: list[DiscoveredReport] = []
        wanted_periods = set(periods_between(period_from, period_to))
        for href, title in parser.links:
            absolute = urljoin(page_url, href)
            if not self._is_allowed_url(absolute):
                continue
            expected_file_type = self._file_type(absolute)
            if expected_file_type not in {"pdf", "xlsx", "zip"}:
                continue
            haystack = f"{title} {absolute}".casefold()
            if "ifrs" not in haystack and "мсфо" not in haystack:
                continue
            if not any(token in haystack for token in ["financial", "result", "statement", "отчет", "результ"]):
                continue
            for period in wanted_periods:
                if self._matches_period(haystack, period):
                    discovered.append(
                        DiscoveredReport(
                            company_ticker=company.ticker,
                            report_period=period,
                            period_from=None,
                            period_to=None,
                            reporting_standard=reporting_standard,
                            document_type="financial_statement",
                            source_role="financial_statements",
                            source_type=self.source_type,
                            source_url=absolute,
                            title=title or absolute.rsplit("/", 1)[-1],
                            language="en" if re.search(r"\bifrs\b", haystack) else None,
                            published_at=None,
                            expected_file_type=expected_file_type,
                            confidence_score=0.75,
                            notes="Discovered from LKOH IR HTML page",
                        )
                    )
        return discovered

    def _is_allowed_url(self, url: str) -> bool:
        parsed = urlparse(url)
        allowed = set(get_settings().report_source_allowed_domains)
        return parsed.scheme in {"http", "https"} and parsed.hostname and parsed.hostname.lower() in allowed

    def _file_type(self, url: str) -> str:
        path = urlparse(url).path.casefold()
        for ext in ["pdf", "xlsx", "zip"]:
            if path.endswith(f".{ext}"):
                return ext
        return "unknown"

    def _matches_period(self, haystack: str, period: str) -> bool:
        year, quarter = period.split("Q")
        q = int(quarter)
        tokens = [
            f"{year}q{q}",
            f"q{q}{year}",
            f"{q}q {year}",
            f"{q} quarter {year}",
            f"{q} квартал {year}",
            f"{q}кв {year}",
        ]
        if q == 4 and ("annual" in haystack or "12m" in haystack or "year" in haystack):
            tokens.append(year)
        return any(token in haystack for token in tokens)
