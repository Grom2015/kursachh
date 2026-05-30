import json
from pathlib import Path

from app.db.models import MetricValue, ReportDocument, StatementFact
from app.services.providers.edisclosure_proof_of_access import (
    EDisclosureProofRequest,
    EDisclosureProofScanner,
)
from app.tools import prove_edisclosure_access as cli


def test_offline_proof_report_has_access_blockers(db_session):
    report = EDisclosureProofScanner(db_session).prove(EDisclosureProofRequest()).to_dict()

    assert report["provider_name"] == "E-Disclosure / Interfax"
    assert report["access_status"] == "requires_auth"
    assert report["authentication_required"] is True
    assert report["paid_or_contract_required"] == "unknown"
    assert report["production_readiness_status"] == "requires_access_check"
    assert "requires_api_credentials" in report["blockers"]
    assert "requires_contract_terms_verification" in report["blockers"]


def test_offline_catalog_docs_are_not_live_verified(db_session):
    report = EDisclosureProofScanner(db_session).prove(EDisclosureProofRequest()).to_dict()

    assert report["offline_only"] is True
    assert report["live_public_docs_check"] is False
    assert report["api_documentation_status"] == "found_in_catalog"
    assert report["api_documentation_live_verified"] is False
    assert report["public_docs_checks"] == []
    assert any("Catalog-known API documentation is not live verification" in item for item in report["limitations"])


def test_no_network_in_offline_mode(monkeypatch, db_session):
    import app.services.providers.edisclosure_proof_of_access as proof_module

    def fail_network(*_args, **_kwargs):
        raise AssertionError("network should not be used in offline proof mode")

    monkeypatch.setattr(proof_module, "urlopen", fail_network)
    report = EDisclosureProofScanner(db_session).prove(
        EDisclosureProofRequest(offline_only=True, live_public_docs_check=False)
    )

    assert report.api_documentation_live_verified is False


def test_sample_tickers_and_missing_identifiers_are_reported(db_session):
    report = EDisclosureProofScanner(db_session).prove(EDisclosureProofRequest()).to_dict()
    by_ticker = {item["ticker"]: item for item in report["sample_readiness"]}

    assert set(by_ticker) == {"TATN", "NVTK", "ROSN", "SIBN"}
    for item in by_ticker.values():
        assert item["issuer_name_available"] is True
        assert item["inn_available"] is False
        assert item["disclosure_id_available"] is False
        assert item["ogrn_available"] is False
        assert item["lookup_readiness"] == "missing_identifier"
        assert "inn" in item["missing_identifiers"]
        assert "disclosure_id" in item["missing_identifiers"]
        assert "ogrn" in item["missing_identifiers"]


def test_live_public_docs_network_failure_is_controlled(monkeypatch, db_session):
    import app.services.providers.edisclosure_proof_of_access as proof_module

    def fail_network(*_args, **_kwargs):
        raise OSError("blocked")

    monkeypatch.setattr(proof_module, "urlopen", fail_network)
    report = EDisclosureProofScanner(db_session).prove(
        EDisclosureProofRequest(offline_only=False, live_public_docs_check=True)
    ).to_dict()

    assert report["api_documentation_live_verified"] is False
    assert report["public_docs_checks"]
    assert all(item["reachable"] is False for item in report["public_docs_checks"])
    assert all(item["failure_reason"] for item in report["public_docs_checks"])


def test_no_pipeline_tables_are_created(db_session):
    before_docs = db_session.query(ReportDocument).count()
    before_facts = db_session.query(StatementFact).count()
    before_metrics = db_session.query(MetricValue).count()

    EDisclosureProofScanner(db_session).prove(EDisclosureProofRequest())

    assert db_session.query(ReportDocument).count() == before_docs
    assert db_session.query(StatementFact).count() == before_facts
    assert db_session.query(MetricValue).count() == before_metrics


def test_cli_writes_json_report(monkeypatch, db_session):
    class SessionContext:
        def __enter__(self):
            return db_session

        def __exit__(self, *_args):
            return None

    runtime_root = Path("tests/runtime_edisclosure_proof")
    original_init = cli.EDisclosureProofScanner.__init__

    def patched_init(self, db=None, root=None):
        original_init(self, db=db, root=runtime_root)

    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "SessionLocal", lambda: SessionContext())
    monkeypatch.setattr(cli.EDisclosureProofScanner, "__init__", patched_init)

    report, path = cli.prove_edisclosure_access(offline_only=True)
    saved = json.loads(Path(path).read_text(encoding="utf-8"))

    assert Path(path).exists()
    assert saved["provider_name"] == "E-Disclosure / Interfax"
    assert report["api_documentation_status"] == "found_in_catalog"
    assert path.endswith("data\\validation\\providers\\edisclosure_proof_of_access.json") or path.endswith(
        "data/validation/providers/edisclosure_proof_of_access.json"
    )


def test_report_does_not_claim_authenticated_capabilities_tested(db_session):
    report = EDisclosureProofScanner(db_session).prove(EDisclosureProofRequest()).to_dict()

    assert "authenticated_api_call" not in report["tested_capabilities"]
    assert "financial_pdf_download" not in report["tested_capabilities"]
    assert "FY 2021 report metadata/document links are not proven obtainable" in " ".join(report["limitations"])
    assert report["recommended_next_action"] == "add_issuer_identifier_enrichment_first"
