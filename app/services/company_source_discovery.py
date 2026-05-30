import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

import httpx
import yaml
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Company
from app.services.company_registry import CompanyRegistry, normalize_query

DISCOVERY_DISCLAIMER = (
    "This discovery report does not verify financial statements. "
    "Run verify_source_candidate / verify_source_package before using candidates in real-data pipeline."
)

BASE_TRUSTED_DOMAINS = {
    "moex.com",
    "iss.moex.com",
    "e-disclosure.ru",
    "www.e-disclosure.ru",
    "disclosure.skrin.ru",
}


@dataclass
class CompanySourceCandidate:
    candidate_type: str
    source_url: str | None
    source_domain: str | None
    source_provider: str
    matched_name: str | None
    ticker: str | None
    confidence_score: float
    confidence_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    verified_live: bool = False
    trust_status: str = "unverified_candidate"
    verification_scope: str = "not_verified"
    auto_apply_allowed: bool = False
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CompanySourceDiscoveryReport:
    query: str
    resolved_company: dict[str, Any] | None
    recommended_candidates: list[CompanySourceCandidate]
    auto_select_allowed: bool
    warnings: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    disclaimer: str = DISCOVERY_DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["recommended_candidates"] = [candidate.to_dict() for candidate in self.recommended_candidates]
        return payload


class MoexIdentityClient:
    def __init__(self, base_url: str | None = None, timeout: float = 10.0):
        self.base_url = (base_url or get_settings().moex_base_url).rstrip("/")
        self.timeout = timeout

    def search_securities(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        url = f"{self.base_url}/securities.json"
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            response = client.get(url, params={"q": query, "iss.meta": "off", "limit": limit})
        response.raise_for_status()
        return self._parse_securities(response.json())[:limit]

    def _parse_securities(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        block = payload.get("securities") or {}
        columns = [str(item).lower() for item in block.get("columns") or []]
        rows = block.get("data") or []
        securities = []
        for row in rows:
            item = {columns[index]: value for index, value in enumerate(row) if index < len(columns)}
            securities.append(
                {
                    "ticker": item.get("secid") or item.get("ticker"),
                    "isin": item.get("isin"),
                    "name": item.get("name") or item.get("shortname") or item.get("emitent_title"),
                    "short_name": item.get("shortname"),
                    "board": item.get("primary_boardid") or item.get("boardid"),
                    "source": "MOEX ISS securities metadata",
                }
            )
        return [item for item in securities if item.get("ticker") or item.get("name")]


class CompanySourceDiscoveryService:
    def __init__(
        self,
        db: Session,
        root: Path | None = None,
        moex_client: MoexIdentityClient | None = None,
        http_client_factory: Any | None = None,
    ):
        self.db = db
        self.root = root or get_settings().root_dir
        self.moex_client = moex_client or MoexIdentityClient()
        self.http_client_factory = http_client_factory or httpx.Client

    def discover(
        self,
        query: str,
        ticker: str | None = None,
        market: str = "MOEX",
        limit: int = 10,
        live: bool = False,
    ) -> CompanySourceDiscoveryReport:
        warnings: list[str] = []
        registry_matches = CompanyRegistry(self.db).search(ticker or query, limit=limit)
        resolved_company = self._resolved_company(registry_matches)
        ambiguous_identity = len(registry_matches) > 1
        if ambiguous_identity:
            warnings.append("Multiple registry matches found; auto selection is disabled.")

        candidates: list[CompanySourceCandidate] = []
        for company in registry_matches:
            candidates.extend(self._registry_candidates(company, ambiguous_identity))

        moex_candidates, moex_warnings = self._moex_identity_candidates(ticker or query, market, limit, ambiguous_identity)
        candidates.extend(moex_candidates)
        warnings.extend(moex_warnings)

        companies_for_source_pages = registry_matches[:1] if registry_matches else []
        for company in companies_for_source_pages:
            candidates.extend(self._source_page_candidates(company, query))
        if not registry_matches:
            candidates.extend(self._generic_disclosure_candidates(query, ticker))

        trusted_domains = self.trusted_domains()
        for candidate in candidates:
            if candidate.source_domain and candidate.source_domain not in trusted_domains:
                candidate.trust_status = "rejected"
                candidate.warnings.append("Candidate domain is not in trusted source discovery allowlist.")
            if ambiguous_identity and candidate.trust_status == "trusted_candidate":
                candidate.trust_status = "ambiguous"
                candidate.warnings.append("Candidate is ambiguous because identity resolution returned multiple companies.")

        if live:
            self._apply_live_metadata(candidates)

        recommended = sorted(
            [item for item in candidates if item.trust_status not in {"rejected"}],
            key=lambda item: (item.confidence_score, item.source_provider, item.source_url or ""),
            reverse=True,
        )[:limit]
        auto_select_allowed = self._auto_select_allowed(recommended, warnings)
        next_actions = [
            "Review recommended candidates manually before updating Company registry fields.",
            "Run verify_source_candidate / verify_source_package before using any URL in the real-data pipeline.",
        ]
        return CompanySourceDiscoveryReport(
            query=query,
            resolved_company=resolved_company,
            recommended_candidates=recommended,
            auto_select_allowed=auto_select_allowed,
            warnings=warnings,
            next_actions=next_actions,
        )

    def save_report(self, report: CompanySourceDiscoveryReport) -> Path:
        output_dir = self.root / "data" / "validation" / "company_source_discovery"
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{self._slug(report.query)}_source_discovery_report.json"
        path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def trusted_domains(self) -> set[str]:
        domains = set(BASE_TRUSTED_DOMAINS)
        domains.update(domain.lower() for domain in get_settings().report_source_allowed_domains)
        domains.update(self._domains_from_seed_registry())
        domains.update(self._domains_from_manifests())
        return {domain for domain in domains if domain and domain != "disclosure.ru"}

    def _registry_candidates(self, company: Company, ambiguous: bool) -> list[CompanySourceCandidate]:
        status = "ambiguous" if ambiguous else "trusted_candidate"
        candidates = [
            CompanySourceCandidate(
                candidate_type="identity",
                source_url=None,
                source_domain=None,
                source_provider="company_registry",
                matched_name=company.full_name,
                ticker=company.ticker,
                confidence_score=0.95,
                confidence_reasons=["Existing active company registry match."],
                warnings=["MOEX/registry identity is not a financial statement source."],
                trust_status=status,
                verification_scope="identity_only",
                provenance={
                    "ticker": company.ticker,
                    "board": company.board,
                    "isin": company.isin,
                    "short_name": company.short_name,
                    "full_name": company.full_name,
                    "disclosure_id": company.disclosure_id,
                },
            )
        ]
        if company.ir_url:
            domain = self._domain(company.ir_url)
            candidates.append(
                CompanySourceCandidate(
                    candidate_type="issuer_ir_page",
                    source_url=company.ir_url,
                    source_domain=domain,
                    source_provider="company_registry",
                    matched_name=company.full_name,
                    ticker=company.ticker,
                    confidence_score=0.9,
                    confidence_reasons=["IR URL is already present in company registry."],
                    trust_status=status,
                    verification_scope="source_page_metadata",
                    provenance={"registry_field": "ir_url"},
                )
            )
        return candidates

    def _moex_identity_candidates(
        self, query: str, market: str, limit: int, registry_ambiguous: bool
    ) -> tuple[list[CompanySourceCandidate], list[str]]:
        if market.upper() != "MOEX":
            return [], [f"Unsupported market for identity discovery: {market}"]
        try:
            securities = self.moex_client.search_securities(query, limit=limit)
        except Exception as exc:
            return [], [f"MOEX identity lookup failed: {exc}"]
        ambiguous = registry_ambiguous or len(securities) > 1
        status = "ambiguous" if ambiguous else "trusted_candidate"
        candidates = []
        for item in securities:
            candidates.append(
                CompanySourceCandidate(
                    candidate_type="identity",
                    source_url="https://iss.moex.com/iss/securities.json",
                    source_domain="iss.moex.com",
                    source_provider="moex_iss",
                    matched_name=item.get("name") or item.get("short_name"),
                    ticker=item.get("ticker"),
                    confidence_score=0.85 if not ambiguous else 0.7,
                    confidence_reasons=["MOEX ISS securities metadata matched query."],
                    warnings=["MOEX identity metadata is not a source of financial statements."],
                    trust_status=status,
                    verification_scope="identity_only",
                    provenance=item,
                )
            )
        return candidates, []

    def _source_page_candidates(self, company: Company, query: str) -> list[CompanySourceCandidate]:
        candidates = []
        if company.disclosure_id:
            url = f"https://www.e-disclosure.ru/portal/company.aspx?id={company.disclosure_id}"
            candidates.append(
                self._disclosure_candidate(
                    url,
                    "e-disclosure",
                    company.full_name,
                    company.ticker,
                    0.88,
                    "disclosure_id",
                )
            )
        candidates.extend(self._generic_disclosure_candidates(query, company.ticker, company.full_name))
        return candidates

    def _generic_disclosure_candidates(
        self, query: str, ticker: str | None = None, matched_name: str | None = None
    ) -> list[CompanySourceCandidate]:
        encoded = quote_plus(query)
        candidates = [
            self._disclosure_candidate(
                f"https://www.e-disclosure.ru/poisk-po-kompaniyam?query={encoded}",
                "e-disclosure",
                matched_name or query,
                ticker,
                0.65,
                "search_query",
            ),
            self._disclosure_candidate(
                f"https://disclosure.skrin.ru/issuers?search={encoded}",
                "disclosure_skrin",
                matched_name or query,
                ticker,
                0.55,
                "search_query",
            ),
        ]
        return candidates

    def _disclosure_candidate(
        self,
        url: str,
        provider: str,
        matched_name: str | None,
        ticker: str | None,
        confidence: float,
        provenance_type: str,
    ) -> CompanySourceCandidate:
        return CompanySourceCandidate(
            candidate_type="disclosure_page",
            source_url=url,
            source_domain=self._domain(url),
            source_provider=provider,
            matched_name=matched_name,
            ticker=ticker,
            confidence_score=confidence,
            confidence_reasons=["Trusted disclosure/source-page candidate.", f"Candidate generated from {provenance_type}."],
            warnings=["This is source-page metadata discovery, not financial statement verification."],
            trust_status="trusted_candidate",
            verification_scope="source_page_metadata",
            provenance={"candidate_origin": provenance_type},
        )

    def _apply_live_metadata(self, candidates: list[CompanySourceCandidate]) -> None:
        for candidate in candidates:
            if not candidate.source_url or candidate.trust_status in {"rejected"}:
                continue
            ok, warnings = self._check_live_metadata(candidate.source_url)
            candidate.verified_live = ok
            candidate.warnings.extend(warnings)
            if not ok:
                candidate.trust_status = "failed_live_check"
            if candidate.verification_scope == "financial_document":
                candidate.verification_scope = "source_page_metadata"
                candidate.warnings.append("Live URL availability does not verify financial-document content.")

    def _check_live_metadata(self, url: str) -> tuple[bool, list[str]]:
        try:
            with self.http_client_factory(timeout=15, follow_redirects=True) as client:
                response = client.head(url)
                if response.status_code in {405, 403, 404}:
                    response = client.get(url, headers={"Range": "bytes=0-0"})
                if response.status_code >= 400:
                    return False, [f"Live metadata check failed with HTTP {response.status_code}."]
                return True, ["Live metadata check succeeded; this verifies URL availability only."]
        except Exception as exc:
            return False, [f"Live metadata check failed: {exc}"]

    def _auto_select_allowed(self, recommended: list[CompanySourceCandidate], warnings: list[str]) -> bool:
        if len(recommended) != 1:
            return False
        candidate = recommended[0]
        if candidate.trust_status in {"ambiguous", "failed_live_check", "rejected"}:
            return False
        if candidate.confidence_score < 0.85:
            return False
        return not any("Multiple" in warning or "ambiguous" in warning.casefold() for warning in warnings)

    def _resolved_company(self, matches: list[Company]) -> dict[str, Any] | None:
        if len(matches) != 1:
            return None
        company = matches[0]
        return {
            "ticker": company.ticker,
            "board": company.board,
            "isin": company.isin,
            "short_name": company.short_name,
            "full_name": company.full_name,
            "ir_url": company.ir_url,
            "disclosure_id": company.disclosure_id,
        }

    def _domains_from_seed_registry(self) -> set[str]:
        path = self.root / "data" / "seed" / "companies.yml"
        if not path.exists():
            return set()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        domains = set()
        for item in data.get("companies", []) or []:
            if item.get("ir_url"):
                domains.add(self._domain(item["ir_url"]))
        return {domain for domain in domains if domain}

    def _domains_from_manifests(self) -> set[str]:
        manifest_dir = self.root / "data" / "manifests"
        if not manifest_dir.exists():
            return set()
        domains = set()
        for path in manifest_dir.glob("*_real_sources.yml"):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for item in data.get("reports", []) or []:
                if item.get("source_url"):
                    domains.add(self._domain(item["source_url"]))
        return {domain for domain in domains if domain}

    def _domain(self, url: str | None) -> str | None:
        if not url:
            return None
        parsed = urlparse(url)
        return (parsed.netloc or "").casefold() or None

    def _slug(self, value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ_-]+", "_", normalize_query(value))
        return slug.strip("_") or "company"
