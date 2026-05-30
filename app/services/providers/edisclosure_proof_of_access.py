import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Company
from app.services.providers.provider_feasibility_scanner import ProviderFeasibilityItem, provider_catalog


@dataclass
class EDisclosureProofRequest:
    sample_tickers: list[str] = field(default_factory=lambda: ["TATN", "NVTK", "ROSN", "SIBN"])
    year: int = 2021
    reporting_standard: str = "IFRS"
    offline_only: bool = True
    live_public_docs_check: bool = False


@dataclass
class EDisclosureSampleReadiness:
    ticker: str
    company_name: str | None
    inn_available: bool
    ogrn_available: bool
    disclosure_id_available: bool
    issuer_name_available: bool
    lookup_readiness: str
    missing_identifiers: list[str]
    recommended_identifier_source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EDisclosurePublicDocsCheck:
    url: str
    purpose: str
    reachable: bool
    http_status: int | None = None
    content_type: str | None = None
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EDisclosureProofReport:
    generated_at: str
    provider_name: str
    provider_url: str
    sample_tickers: list[str]
    year: int
    reporting_standard: str
    offline_only: bool
    live_public_docs_check: bool
    access_status: str
    api_documentation_status: str
    api_documentation_live_verified: bool
    authentication_required: bool | str
    paid_or_contract_required: bool | str
    production_readiness_status: str
    endpoints_discovered: list[dict[str, Any]]
    required_credentials: list[str]
    expected_capabilities: list[str]
    tested_capabilities: list[str]
    sample_readiness: list[dict[str, Any]]
    public_docs_checks: list[dict[str, Any]]
    blockers: list[str]
    legal_access_questions: list[str]
    redistribution_questions: list[str]
    recommended_next_action: str
    next_actions: list[str]
    limitations: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EDisclosureProofScanner:
    def __init__(self, db: Session | None = None, root: Path | None = None):
        self.db = db
        self.root = root or get_settings().root_dir

    def prove(self, request: EDisclosureProofRequest) -> EDisclosureProofReport:
        provider = _edisclosure_provider()
        docs_checks: list[EDisclosurePublicDocsCheck] = []
        if request.live_public_docs_check and not request.offline_only:
            docs_checks = [self._check_public_url(item["url"], item["purpose"]) for item in self._public_docs_targets(provider)]
        live_verified = bool(docs_checks) and any(check.reachable for check in docs_checks if check.purpose == "api_docs")
        sample_readiness = [item.to_dict() for item in self._sample_readiness(request.sample_tickers)]
        return EDisclosureProofReport(
            generated_at=datetime.now(UTC).isoformat(),
            provider_name=provider.provider_name,
            provider_url=provider.website,
            sample_tickers=[ticker.upper() for ticker in request.sample_tickers],
            year=request.year,
            reporting_standard=request.reporting_standard.upper(),
            offline_only=request.offline_only,
            live_public_docs_check=request.live_public_docs_check,
            access_status="requires_auth",
            api_documentation_status="found_in_catalog",
            api_documentation_live_verified=live_verified,
            authentication_required=True,
            paid_or_contract_required="unknown",
            production_readiness_status="requires_access_check",
            endpoints_discovered=self._endpoints_discovered(provider),
            required_credentials=[
                "E-Disclosure API gateway account",
                "API key or token issued by provider",
                "Contract or written authorization for automated access",
            ],
            expected_capabilities=[
                f"{request.year} issuer report metadata lookup",
                f"{request.year} {request.reporting_standard.upper()} financial reporting message search",
                "issuer/company lookup by disclosure issuer id, INN, or issuer name",
                "report document link metadata if permitted by gateway contract",
                "archive coverage checks for requested sample tickers",
            ],
            tested_capabilities=["curated_catalog_review", "local_registry_identifier_readiness"],
            sample_readiness=sample_readiness,
            public_docs_checks=[item.to_dict() for item in docs_checks],
            blockers=self._blockers(sample_readiness),
            legal_access_questions=[
                "What contract is required for API gateway access?",
                "Are FY 2021 IFRS report metadata and document links available via the gateway?",
                "What rate limits, archive limits, and retention terms apply?",
                "Are automated document-link retrieval and downstream validation allowed?",
            ],
            redistribution_questions=[
                "Can metadata and report links be stored in the product database?",
                "Can downloaded official documents be cached for validation?",
                "Can derived facts/metrics based on provider-retrieved documents be displayed to end users?",
            ],
            recommended_next_action=self._recommended_next_action(sample_readiness),
            next_actions=[
                "Request E-Disclosure API credentials and contract terms.",
                f"Verify archive/document-link access for {', '.join(request.sample_tickers)} FY {request.year}.",
                "Enrich local issuer identifiers before authenticated POC if disclosure ids or INNs are missing.",
                "Run a separate authenticated proof-of-metadata only after credentials and legal terms are approved.",
            ],
            limitations=[
                "Catalog-known API documentation is not live verification.",
                "API existence is not usable access.",
                "No authenticated API calls were attempted.",
                (
                    "No paid calls, financial PDF downloads, parser runs, metric runs, valuation, peer comparison, "
                    "or LLM calls were made."
                ),
                "FY 2021 report metadata/document links are not proven obtainable until authenticated access is tested.",
            ],
        )

    def save_report(self, report: EDisclosureProofReport) -> Path:
        root = self.root / "data" / "validation" / "providers"
        root.mkdir(parents=True, exist_ok=True)
        path = root / "edisclosure_proof_of_access.json"
        path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path

    def _public_docs_targets(self, provider: ProviderFeasibilityItem) -> list[dict[str, str]]:
        urls = provider.source_urls
        return [
            {"url": urls[0], "purpose": "provider_home"},
            {"url": urls[-1], "purpose": "api_docs"},
        ]

    def _check_public_url(self, url: str, purpose: str) -> EDisclosurePublicDocsCheck:
        request = Request(url, method="HEAD", headers={"User-Agent": "moex-edisclosure-proof/0.1"})
        try:
            with urlopen(request, timeout=5) as response:
                return EDisclosurePublicDocsCheck(
                    url=url,
                    purpose=purpose,
                    reachable=True,
                    http_status=response.status,
                    content_type=response.headers.get("content-type"),
                )
        except (OSError, URLError) as exc:
            return EDisclosurePublicDocsCheck(
                url=url,
                purpose=purpose,
                reachable=False,
                failure_reason=str(exc),
            )

    def _sample_readiness(self, tickers: list[str]) -> list[EDisclosureSampleReadiness]:
        companies = self._companies_by_ticker(tickers)
        return [self._readiness_for_ticker(ticker.upper(), companies.get(ticker.upper())) for ticker in tickers]

    def _companies_by_ticker(self, tickers: list[str]) -> dict[str, Company]:
        if self.db is None:
            return {}
        normalized = [ticker.upper() for ticker in tickers]
        companies = self.db.scalars(select(Company).where(Company.ticker.in_(normalized))).all()
        return {company.ticker.upper(): company for company in companies}

    def _readiness_for_ticker(self, ticker: str, company: Company | None) -> EDisclosureSampleReadiness:
        company_name = company.full_name if company else None
        inn_available = bool(company and company.inn)
        disclosure_id_available = bool(company and company.disclosure_id)
        issuer_name_available = bool(company_name)
        missing = []
        if not issuer_name_available:
            missing.append("issuer_name")
        if not inn_available:
            missing.append("inn")
        if not disclosure_id_available:
            missing.append("disclosure_id")
        missing.append("ogrn")
        lookup_readiness = (
            "ready_for_api_test"
            if issuer_name_available and (inn_available or disclosure_id_available)
            else "missing_identifier"
        )
        return EDisclosureSampleReadiness(
            ticker=ticker,
            company_name=company_name,
            inn_available=inn_available,
            ogrn_available=False,
            disclosure_id_available=disclosure_id_available,
            issuer_name_available=issuer_name_available,
            lookup_readiness=lookup_readiness,
            missing_identifiers=missing,
            recommended_identifier_source=(
                "Enrich via local registry, MOEX identity metadata, issuer pages, or E-Disclosure company search."
            ),
        )

    def _endpoints_discovered(self, provider: ProviderFeasibilityItem) -> list[dict[str, Any]]:
        return [
            {
                "url": provider.source_urls[0],
                "purpose": "provider_home",
                "status": "catalog_known_not_live_verified",
            },
            {
                "url": provider.source_urls[-1],
                "purpose": "api_docs_or_swagger",
                "status": "catalog_known_not_live_verified",
            },
            {
                "url": provider.source_urls[1],
                "purpose": "issuer_company_page_template",
                "status": "catalog_known_not_live_verified",
            },
        ]

    def _blockers(self, sample_readiness: list[dict[str, Any]]) -> list[str]:
        blockers = [
            "requires_api_credentials",
            "requires_contract_terms_verification",
            "archive_coverage_unverified",
            "document_link_access_unverified",
            "redistribution_rights_unverified",
        ]
        if any(item["lookup_readiness"] == "missing_identifier" for item in sample_readiness):
            blockers.append("sample_issuer_identifier_enrichment_required")
        return blockers

    def _recommended_next_action(self, sample_readiness: list[dict[str, Any]]) -> str:
        if any(item["lookup_readiness"] == "missing_identifier" for item in sample_readiness):
            return "add_issuer_identifier_enrichment_first"
        return "request_api_credentials_and_contract_terms"


def _edisclosure_provider() -> ProviderFeasibilityItem:
    for item in provider_catalog():
        if item.provider_name == "E-Disclosure / Interfax":
            return item
    raise RuntimeError("E-Disclosure / Interfax provider catalog entry is missing.")
