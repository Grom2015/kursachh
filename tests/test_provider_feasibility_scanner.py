import json
from pathlib import Path

from app.db.models import MetricValue, ReportDocument, StatementFact
from app.services.providers.provider_feasibility_scanner import (
    ProviderFeasibilityRequest,
    ProviderFeasibilityScanner,
    provider_catalog,
)
from app.tools import scan_provider_feasibility as cli


def by_name(report: dict) -> dict[str, dict]:
    return {item["provider_name"]: item for item in report["provider_items"]}


def test_provider_report_includes_required_providers():
    report = ProviderFeasibilityScanner().scan(ProviderFeasibilityRequest()).to_dict()
    providers = by_name(report)

    assert "E-Disclosure / Interfax" in providers
    assert "FNS GIR BO" in providers
    assert "MOEX ISS" in providers
    assert "SKRIN Disclosure" in providers
    assert "AK&M disclosure.ru" in providers
    assert "Fedresurs" in providers
    assert "Kontur.Focus" in providers
    assert "Ofdata" in providers
    assert "Cbonds / RU Data" in providers
    assert "SPARK Interfax" in providers


def test_e_disclosure_api_gateway_is_marked_available_but_not_production_ready():
    providers = by_name(ProviderFeasibilityScanner().scan(ProviderFeasibilityRequest()).to_dict())
    item = providers["E-Disclosure / Interfax"]

    assert item["api_available"] == "yes"
    assert item["api_type"] == "REST"
    assert item["requires_auth"] is True
    assert item["access_terms"] == "requires_verification"
    assert item["likely_paid"] == "unclear"
    assert item["production_ready"] is False
    assert "primary_report_source" in item["role_in_architecture"]


def test_fns_gir_bo_separates_ras_download_and_api_subscription():
    providers = by_name(ProviderFeasibilityScanner().scan(ProviderFeasibilityRequest()).to_dict())
    item = providers["FNS GIR BO"]

    assert item["reporting_standard_coverage"] == "RAS"
    assert "normalized_financials" in item["supported_data_types"]
    assert "report_documents" in item["supported_data_types"]
    assert "IFRS" not in item["reporting_standard_coverage"]
    assert "Free per-company download" in item["license_restrictions"]
    assert item["likely_paid"] is True
    assert item["access_terms"] == "requires_verification"


def test_moex_iss_is_identity_and_market_provider_not_report_source():
    providers = by_name(ProviderFeasibilityScanner().scan(ProviderFeasibilityRequest()).to_dict())
    item = providers["MOEX ISS"]

    assert item["api_available"] == "yes"
    assert item["requires_auth"] is False
    assert set(item["supported_data_types"]) == {"company_identity", "market_data"}
    assert item["reporting_standard_coverage"] == "none"
    assert "company_identity_provider" in item["role_in_architecture"]
    assert "market_data_provider" in item["role_in_architecture"]
    assert "primary_report_source" not in item["role_in_architecture"]


def test_commercial_providers_have_cost_and_license_risk():
    providers = by_name(ProviderFeasibilityScanner().scan(ProviderFeasibilityRequest()).to_dict())
    for name in ["Kontur.Focus", "Ofdata", "Cbonds / RU Data", "SPARK Interfax"]:
        item = providers[name]
        assert item["provider_type"] == "commercial_data_provider"
        assert item["likely_paid"] is True
        assert item["production_ready"] is False
        assert any("license" in risk.casefold() or "cost" in risk.casefold() for risk in item["risks"])


def test_unknown_capabilities_remain_unclear_not_assumed():
    providers = by_name(ProviderFeasibilityScanner().scan(ProviderFeasibilityRequest()).to_dict())

    assert providers["SKRIN Disclosure"]["api_available"] == "unclear"
    assert providers["AK&M disclosure.ru"]["api_available"] == "unclear"
    assert providers["Fedresurs"]["api_available"] == "unclear"
    assert providers["SPARK Interfax"]["api_available"] == "unclear"


def test_report_includes_architecture_and_best_candidates():
    report = ProviderFeasibilityScanner().scan(ProviderFeasibilityRequest()).to_dict()

    strategy = report["recommended_provider_strategy"]
    assert "CompanyIdentityProvider" in strategy["provider_layer_interfaces"]
    assert "ReportMetadataProvider" in strategy["provider_layer_interfaces"]
    assert "ValuationInputProvider" in strategy["provider_layer_interfaces"]
    assert report["best_candidates_by_data_type"]["market_data"][0]["provider_name"] == "MOEX ISS"
    assert any(item["provider_name"] == "FNS GIR BO" for item in report["best_candidates_by_data_type"]["normalized_financials"])
    assert any(
        item["provider_name"] == "E-Disclosure / Interfax"
        for item in report["best_candidates_by_data_type"]["report_metadata"]
    )


def test_scanner_requires_no_secrets_and_stays_offline_by_default(monkeypatch):
    import app.services.providers.provider_feasibility_scanner as scanner_module

    def fail_network(*_args, **_kwargs):
        raise AssertionError("network should not be used in offline mode")

    monkeypatch.setattr(scanner_module, "urlopen", fail_network)
    report = ProviderFeasibilityScanner().scan(ProviderFeasibilityRequest(offline_only=True, live_metadata_check=False))

    assert report.offline_only is True
    assert report.live_metadata_check is False
    assert all(item["live_metadata_status"] == "not_checked" for item in report.to_dict()["provider_items"])


def test_scanner_does_not_mutate_db_or_pipeline_tables(db_session):
    before_docs = db_session.query(ReportDocument).count()
    before_facts = db_session.query(StatementFact).count()
    before_metrics = db_session.query(MetricValue).count()

    ProviderFeasibilityScanner().scan(ProviderFeasibilityRequest())

    assert db_session.query(ReportDocument).count() == before_docs
    assert db_session.query(StatementFact).count() == before_facts
    assert db_session.query(MetricValue).count() == before_metrics


def test_cli_writes_json_report(monkeypatch):
    original_init = cli.ProviderFeasibilityScanner.__init__
    runtime_root = Path("tests/runtime_provider_feasibility_scanner")

    def patched_init(self, root=None):
        original_init(self, root=runtime_root)

    monkeypatch.setattr(cli.ProviderFeasibilityScanner, "__init__", patched_init)
    report, path = cli.scan_provider_feasibility(market="MOEX", offline_only=True)

    saved = json.loads(Path(path).read_text(encoding="utf-8"))
    assert Path(path).exists()
    assert path.endswith("data\\validation\\providers\\provider_feasibility_scan.json") or path.endswith(
        "data/validation/providers/provider_feasibility_scan.json"
    )
    assert saved["providers_checked"] == len(provider_catalog())
    assert report["summary"]["production_ready_count"] == 0
    assert "recommended_provider_strategy" in report


def test_provider_filter_keeps_requested_catalog_items_only():
    report = ProviderFeasibilityScanner().scan(
        ProviderFeasibilityRequest(providers=["MOEX ISS", "FNS GIR BO"])
    )

    assert [item["provider_name"] for item in report.to_dict()["provider_items"]] == ["FNS GIR BO", "MOEX ISS"]
