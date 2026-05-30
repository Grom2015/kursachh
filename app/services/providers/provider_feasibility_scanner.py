import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from app.core.config import get_settings


@dataclass
class ProviderFeasibilityRequest:
    market: str = "MOEX"
    providers: list[str] | None = None
    reporting_standards: list[str] = field(default_factory=lambda: ["IFRS", "RAS"])
    data_types: list[str] = field(
        default_factory=lambda: [
            "report_metadata",
            "report_documents",
            "normalized_financials",
            "market_data",
            "valuation_inputs",
            "dividends",
            "company_identity",
        ]
    )
    sample_tickers: list[str] = field(default_factory=lambda: ["LKOH", "TATN", "GAZP", "NVTK", "ROSN", "SIBN"])
    offline_only: bool = True
    live_metadata_check: bool = False


@dataclass
class ProviderFeasibilityItem:
    provider_name: str
    provider_type: str
    website: str
    source_urls: list[str]
    api_available: str
    api_type: str
    requires_auth: bool | str
    likely_paid: bool | str
    access_terms: str
    license_restrictions: str | None
    supported_data_types: list[str]
    reporting_standard_coverage: str
    historical_depth: str
    document_access: str
    machine_readability: str
    coverage_estimate: str
    integration_difficulty: str
    trust_level: str
    role_in_architecture: list[str]
    production_ready: bool
    risks: list[str]
    open_questions: list[str]
    recommended_next_action: str
    live_metadata_status: str = "not_checked"
    live_metadata_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderFeasibilityReport:
    generated_at: str
    market: str
    offline_only: bool
    live_metadata_check: bool
    providers_checked: int
    provider_items: list[dict[str, Any]]
    summary: dict[str, Any]
    best_candidates_by_data_type: dict[str, list[dict[str, Any]]]
    provider_comparison_table: list[dict[str, Any]]
    recommended_provider_strategy: dict[str, Any]
    risks: list[str]
    next_actions: list[str]
    limitations: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProviderFeasibilityScanner:
    def __init__(self, root: Path | None = None):
        self.root = root or get_settings().root_dir

    def scan(self, request: ProviderFeasibilityRequest) -> ProviderFeasibilityReport:
        items = self._provider_items(request)
        if request.live_metadata_check and not request.offline_only:
            items = [self._with_live_metadata_status(item) for item in items]
        provider_dicts = [item.to_dict() for item in items]
        return ProviderFeasibilityReport(
            generated_at=datetime.now(UTC).isoformat(),
            market=request.market.upper(),
            offline_only=request.offline_only,
            live_metadata_check=request.live_metadata_check,
            providers_checked=len(items),
            provider_items=provider_dicts,
            summary=self._summary(items),
            best_candidates_by_data_type=self._best_candidates_by_data_type(items, request.data_types),
            provider_comparison_table=self._comparison_table(items),
            recommended_provider_strategy=self._recommended_provider_strategy(items),
            risks=self._risks(items),
            next_actions=self._next_actions(),
            limitations=[
                "Provider feasibility scan is report-only and does not prove provider production usability.",
                "No paid/API/authenticated requests are made and no secrets are required.",
                "Auth, pricing, limits, historical completeness, and redistribution rights require separate verification.",
                "The current internal pipeline remains primary until provider access and quality are proven.",
            ],
        )

    def save_report(self, report: ProviderFeasibilityReport) -> Path:
        root = self.root / "data" / "validation" / "providers"
        root.mkdir(parents=True, exist_ok=True)
        path = root / "provider_feasibility_scan.json"
        path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path

    def _provider_items(self, request: ProviderFeasibilityRequest) -> list[ProviderFeasibilityItem]:
        requested = {provider.casefold() for provider in request.providers or []}
        items = provider_catalog()
        if requested:
            items = [item for item in items if item.provider_name.casefold() in requested]
        return items

    def _with_live_metadata_status(self, item: ProviderFeasibilityItem) -> ProviderFeasibilityItem:
        checked = []
        warnings = []
        for url in item.source_urls[:2]:
            try:
                request = Request(url, method="HEAD", headers={"User-Agent": "moex-provider-feasibility-scan/0.1"})
                with urlopen(request, timeout=5) as response:
                    checked.append({"url": url, "status_code": response.status})
            except (OSError, URLError) as exc:
                warnings.append(f"{url}: {exc}")
        item.live_metadata_status = "reachable" if checked else "unreachable_or_blocked"
        item.live_metadata_warnings = warnings
        return item

    def _summary(self, items: list[ProviderFeasibilityItem]) -> dict[str, Any]:
        by_type: dict[str, int] = defaultdict(int)
        by_trust: dict[str, int] = defaultdict(int)
        api_available: dict[str, int] = defaultdict(int)
        production_ready = 0
        for item in items:
            by_type[item.provider_type] += 1
            by_trust[item.trust_level] += 1
            api_available[item.api_available] += 1
            production_ready += int(item.production_ready)
        return {
            "providers_checked": len(items),
            "provider_type_counts": dict(by_type),
            "trust_level_counts": dict(by_trust),
            "api_available_counts": dict(api_available),
            "production_ready_count": production_ready,
            "requires_access_verification_count": sum(1 for item in items if item.access_terms == "requires_verification"),
            "commercial_or_paid_risk_count": sum(
                1 for item in items if item.likely_paid is True or item.likely_paid == "unclear"
            ),
        }

    def _best_candidates_by_data_type(
        self, items: list[ProviderFeasibilityItem], data_types: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for data_type in data_types:
            candidates = [
                {
                    "provider_name": item.provider_name,
                    "role_in_architecture": item.role_in_architecture,
                    "trust_level": item.trust_level,
                    "access_terms": item.access_terms,
                    "production_ready": item.production_ready,
                    "recommended_next_action": item.recommended_next_action,
                }
                for item in items
                if data_type in item.supported_data_types
            ]
            result[data_type] = sorted(
                candidates,
                key=lambda item: _candidate_priority(item["trust_level"], item["access_terms"]),
            )
        return result

    def _comparison_table(self, items: list[ProviderFeasibilityItem]) -> list[dict[str, Any]]:
        return [
            {
                "provider_name": item.provider_name,
                "provider_type": item.provider_type,
                "api_available": item.api_available,
                "api_type": item.api_type,
                "requires_auth": item.requires_auth,
                "likely_paid": item.likely_paid,
                "access_terms": item.access_terms,
                "supported_data_types": item.supported_data_types,
                "reporting_standard_coverage": item.reporting_standard_coverage,
                "document_access": item.document_access,
                "machine_readability": item.machine_readability,
                "trust_level": item.trust_level,
                "production_ready": item.production_ready,
            }
            for item in items
        ]

    def _recommended_provider_strategy(self, items: list[ProviderFeasibilityItem]) -> dict[str, Any]:
        return {
            "provider_layer_interfaces": [
                "CompanyIdentityProvider",
                "ReportMetadataProvider",
                "ReportDocumentProvider",
                "NormalizedFinancialsProvider",
                "MarketDataProvider",
                "ValuationInputProvider",
            ],
            "suggested_roles": {
                "CompanyIdentityProvider": ["MOEX ISS", "FNS GIR BO"],
                "ReportMetadataProvider": ["E-Disclosure / Interfax", "SKRIN Disclosure", "AK&M disclosure.ru"],
                "ReportDocumentProvider": ["E-Disclosure / Interfax", "SKRIN Disclosure", "issuer sites"],
                "NormalizedFinancialsProvider": ["FNS GIR BO", "Ofdata", "Kontur.Focus", "Cbonds / RU Data", "SPARK"],
                "MarketDataProvider": ["MOEX ISS", "Cbonds / RU Data"],
                "ValuationInputProvider": ["Cbonds / RU Data", "MOEX ISS plus separate shares/dividend inputs"],
            },
            "primary_recommendation": (
                "First verify E-Disclosure gateway access for official report metadata/documents, then separately "
                "evaluate FNS GIR BO or licensed commercial APIs for structured RAS financials."
            ),
        }

    def _risks(self, items: list[ProviderFeasibilityItem]) -> list[str]:
        risks = sorted({risk for item in items for risk in item.risks})
        return [
            *risks,
            "Provider data must not replace document validation and quality gates until quality is proven.",
            "Redistribution and derivative-data rights must be reviewed before product use.",
        ]

    def _next_actions(self) -> list[str]:
        return [
            "Request/verify E-Disclosure API gateway terms, Swagger access, limits, and archival document coverage.",
            "Test FNS GIR BO free per-company download and paid/API subscription path on sample INNs.",
            "Run a commercial-provider proof-of-access for normalized RAS/fundamentals only after license terms are clear.",
            "Keep MOEX ISS in identity/market-data modules and do not treat it as a financial-report provider.",
        ]


def _candidate_priority(trust_level: str, access_terms: str) -> tuple[int, int]:
    trust_rank = {"official": 0, "regulated": 1, "commercial": 2, "unknown": 3, "unofficial": 4}
    access_rank = {"public": 0, "requires_verification": 1, "unknown": 2, "not_recommended": 3}
    return trust_rank.get(trust_level, 9), access_rank.get(access_terms, 9)


def provider_catalog() -> list[ProviderFeasibilityItem]:
    return [
        ProviderFeasibilityItem(
            provider_name="E-Disclosure / Interfax",
            provider_type="official_disclosure",
            website="https://www.e-disclosure.ru/",
            source_urls=[
                "https://www.e-disclosure.ru/",
                "https://www.e-disclosure.ru/portal/company.aspx",
                "https://www.e-disclosure.ru/api/v1/swagger/index.html",
            ],
            api_available="yes",
            api_type="REST",
            requires_auth=True,
            likely_paid="unclear",
            access_terms="requires_verification",
            license_restrictions=(
                "Automated gateway access, archive depth, limits, and reuse rights require contract verification."
            ),
            supported_data_types=["report_metadata", "report_documents", "company_identity"],
            reporting_standard_coverage="both",
            historical_depth="archive_available_requires_verification",
            document_access="document_links",
            machine_readability="mixed",
            coverage_estimate="high",
            integration_difficulty="medium",
            trust_level="official",
            role_in_architecture=["primary_report_source", "report_metadata_provider", "report_document_provider"],
            production_ready=False,
            risks=["requires authorized API gateway access", "license and redistribution rights unclear"],
            open_questions=[
                "What contract is required for API gateway access?",
                "Are 2021 IFRS/RAS archives available through the gateway?",
                "Are document URLs stable and reusable in a commercial product?",
            ],
            recommended_next_action="Verify API gateway contract, Swagger access, limits, archive depth, and reuse rights.",
        ),
        ProviderFeasibilityItem(
            provider_name="FNS GIR BO",
            provider_type="government_registry",
            website="https://bo.nalog.gov.ru/",
            source_urls=[
                "https://www.nalog.gov.ru/rn77/bo/",
                "https://bo.nalog.gov.ru/",
                "https://bo.nalog.gov.ru/subscribe/",
            ],
            api_available="yes",
            api_type="REST",
            requires_auth=True,
            likely_paid=True,
            access_terms="requires_verification",
            license_restrictions=(
                "Free per-company download is separate from paid/API subscription; redistribution and bulk use require review."
            ),
            supported_data_types=["normalized_financials", "report_documents", "company_identity"],
            reporting_standard_coverage="RAS",
            historical_depth="annual_accounting_reports_since_2019_plus_legacy_sources_require_verification",
            document_access="downloadable_documents",
            machine_readability="mixed",
            coverage_estimate="high",
            integration_difficulty="medium",
            trust_level="official",
            role_in_architecture=["ras_source", "normalized_financials_provider", "company_identity_provider"],
            production_ready=False,
            risks=["RAS only for this architecture", "paid API/subscription terms require verification"],
            open_questions=[
                "Which public companies restrict access to accounting reports?",
                "What exact subscription/API limits and formats apply?",
                "Can downloaded reports/data be reused in the product?",
            ],
            recommended_next_action="Test free per-company download and verify paid/API subscription terms on sample INNs.",
        ),
        ProviderFeasibilityItem(
            provider_name="MOEX ISS",
            provider_type="exchange_api",
            website="https://iss.moex.com/iss/reference/",
            source_urls=["https://iss.moex.com/iss/reference/", "https://iss.moex.com/iss/engines/stock/markets/shares/"],
            api_available="yes",
            api_type="REST",
            requires_auth=False,
            likely_paid=False,
            access_terms="public",
            license_restrictions="Market data usage and redistribution terms still require review.",
            supported_data_types=["company_identity", "market_data"],
            reporting_standard_coverage="none",
            historical_depth="market_history_available_by_board_and_security",
            document_access="metadata_only",
            machine_readability="structured_json",
            coverage_estimate="high",
            integration_difficulty="low",
            trust_level="official",
            role_in_architecture=["company_identity_provider", "market_data_provider"],
            production_ready=False,
            risks=["not a financial reporting source", "does not provide IFRS/RAS statement documents"],
            open_questions=["Which market-cap/shares-outstanding inputs are reliable enough for valuation metrics?"],
            recommended_next_action="Keep as identity and market-data provider; do not use for financial report discovery.",
        ),
        ProviderFeasibilityItem(
            provider_name="SKRIN Disclosure",
            provider_type="official_disclosure",
            website="https://disclosure.skrin.ru/",
            source_urls=["https://disclosure.skrin.ru/", "https://disclosure.skrin.ru/index.asp"],
            api_available="unclear",
            api_type="unknown",
            requires_auth="unknown",
            likely_paid="unclear",
            access_terms="requires_verification",
            license_restrictions="Access, API availability, and reuse rights require verification with SKRIN.",
            supported_data_types=["report_metadata", "report_documents"],
            reporting_standard_coverage="both",
            historical_depth="unclear",
            document_access="document_links",
            machine_readability="mixed",
            coverage_estimate="medium",
            integration_difficulty="high",
            trust_level="official",
            role_in_architecture=["secondary_report_source", "report_metadata_provider"],
            production_ready=False,
            risks=["portal/auth terms unclear", "automated access rights unclear"],
            open_questions=["Is there a documented API for report metadata/documents?", "What archive depth is available?"],
            recommended_next_action="Contact/verify SKRIN API or export access before integration work.",
        ),
        ProviderFeasibilityItem(
            provider_name="AK&M disclosure.ru",
            provider_type="official_disclosure",
            website="https://www.disclosure.ru/",
            source_urls=["https://www.disclosure.ru/", "https://disclosure.ru/"],
            api_available="unclear",
            api_type="web_portal_only",
            requires_auth="unknown",
            likely_paid="unclear",
            access_terms="requires_verification",
            license_restrictions="Automated retrieval and reuse rights require verification.",
            supported_data_types=["report_metadata", "report_documents"],
            reporting_standard_coverage="both",
            historical_depth="unclear",
            document_access="document_links",
            machine_readability="mixed",
            coverage_estimate="medium",
            integration_difficulty="high",
            trust_level="official",
            role_in_architecture=["secondary_report_source"],
            production_ready=False,
            risks=["API not confirmed", "web portal format may be unstable for automation"],
            open_questions=[
                "Is there official API/export access?",
                "Can archived report documents be retrieved programmatically?",
            ],
            recommended_next_action="Treat as fallback disclosure source until API/export terms are proven.",
        ),
        ProviderFeasibilityItem(
            provider_name="Fedresurs",
            provider_type="legal_disclosure_event_source",
            website="https://fedresurs.ru/",
            source_urls=["https://fedresurs.ru/"],
            api_available="unclear",
            api_type="unknown",
            requires_auth="unknown",
            likely_paid="unclear",
            access_terms="requires_verification",
            license_restrictions="Event data access and reuse rights require verification.",
            supported_data_types=["report_metadata", "company_identity"],
            reporting_standard_coverage="unclear",
            historical_depth="unclear",
            document_access="metadata_only",
            machine_readability="mixed",
            coverage_estimate="medium",
            integration_difficulty="medium",
            trust_level="official",
            role_in_architecture=["disclosure_event_source", "identity_source"],
            production_ready=False,
            risks=["event corroboration only", "not a primary financial statement source"],
            open_questions=["Can report-disclosure events be reliably filtered by issuer and period?"],
            recommended_next_action="Use only as disclosure event corroboration unless document/API access is proven.",
        ),
        ProviderFeasibilityItem(
            provider_name="Kontur.Focus",
            provider_type="commercial_data_provider",
            website="https://focus.kontur.ru/",
            source_urls=[
                "https://focus.kontur.ru/",
                "https://focus.kontur.ru/site/capabilities/financial-statements",
                "https://focus.kontur.ru/site/news/8376",
            ],
            api_available="yes",
            api_type="REST",
            requires_auth=True,
            likely_paid=True,
            access_terms="requires_verification",
            license_restrictions="Commercial API; redistribution and derived-product rights require contract review.",
            supported_data_types=["normalized_financials", "company_identity"],
            reporting_standard_coverage="RAS",
            historical_depth="unclear",
            document_access="normalized_data",
            machine_readability="structured_json",
            coverage_estimate="high",
            integration_difficulty="medium",
            trust_level="commercial",
            role_in_architecture=["normalized_financials_provider"],
            production_ready=False,
            risks=["commercial license/cost risk", "RAS-focused counterparty data, not IFRS consolidated source"],
            open_questions=[
                "Does the API license allow product redistribution?",
                "What exact financial statement fields are available?",
            ],
            recommended_next_action="Evaluate only after licensing and redistribution terms are explicit.",
        ),
        ProviderFeasibilityItem(
            provider_name="Ofdata",
            provider_type="commercial_data_provider",
            website="https://ofdata.ru/",
            source_urls=["https://ofdata.ru/api", "https://ofdata.ru/api/finances"],
            api_available="yes",
            api_type="REST",
            requires_auth=True,
            likely_paid=True,
            access_terms="requires_verification",
            license_restrictions="API key required; cost, rate limits, and redistribution rights require verification.",
            supported_data_types=["normalized_financials", "company_identity"],
            reporting_standard_coverage="RAS",
            historical_depth="2011_plus_requires_verification",
            document_access="normalized_data",
            machine_readability="structured_json",
            coverage_estimate="medium",
            integration_difficulty="low",
            trust_level="commercial",
            role_in_architecture=["normalized_financials_provider"],
            production_ready=False,
            risks=["commercial license/cost risk", "source derived from FNS/Rosstat and not IFRS consolidated"],
            open_questions=["Can data be redistributed in the product?", "Are all target issuers and years covered?"],
            recommended_next_action="Run proof-of-access only after API key, pricing, and license scope are approved.",
        ),
        ProviderFeasibilityItem(
            provider_name="Cbonds / RU Data",
            provider_type="commercial_data_provider",
            website="https://cbonds.com/api/reports/",
            source_urls=["https://cbonds.com/api/", "https://cbonds.com/api/reports/", "https://cbonds.ru/api/reports/"],
            api_available="yes",
            api_type="REST/SOAP/file_export",
            requires_auth=True,
            likely_paid=True,
            access_terms="requires_verification",
            license_restrictions=(
                "Commercial API/data feed; redistribution, display, and derived-data rights require contract review."
            ),
            supported_data_types=[
                "normalized_financials",
                "market_data",
                "valuation_inputs",
                "dividends",
                "company_identity",
            ],
            reporting_standard_coverage="IFRS",
            historical_depth="since_2000_claim_requires_sample_verification",
            document_access="normalized_data",
            machine_readability="mixed",
            coverage_estimate="high",
            integration_difficulty="medium",
            trust_level="commercial",
            role_in_architecture=["normalized_financials_provider", "valuation_input_source", "market_data_source"],
            production_ready=False,
            risks=["commercial license/cost risk", "must validate Russian public company coverage and methodology"],
            open_questions=[
                "Which MOEX issuers have IFRS statements?",
                "Can metrics/data be redistributed or shown to end users?",
            ],
            recommended_next_action="Request demo/sample for LKOH/TATN/GAZP and review methodology/license before integration.",
        ),
        ProviderFeasibilityItem(
            provider_name="SPARK Interfax",
            provider_type="commercial_data_provider",
            website="https://spark-interfax.ru/",
            source_urls=["https://spark-interfax.ru/"],
            api_available="unclear",
            api_type="unknown",
            requires_auth=True,
            likely_paid=True,
            access_terms="requires_verification",
            license_restrictions="Commercial service; API, redistribution, and product-display rights require contract review.",
            supported_data_types=["normalized_financials", "company_identity"],
            reporting_standard_coverage="RAS",
            historical_depth="unclear",
            document_access="normalized_data",
            machine_readability="unclear",
            coverage_estimate="high",
            integration_difficulty="high",
            trust_level="commercial",
            role_in_architecture=["normalized_financials_provider", "identity_source"],
            production_ready=False,
            risks=["commercial license/cost risk", "API/data fields not confirmed from public docs"],
            open_questions=[
                "Is API access available for the required data?",
                "What fields, limits, and redistribution rights apply?",
            ],
            recommended_next_action="Treat as commercial candidate requiring direct vendor verification.",
        ),
    ]
