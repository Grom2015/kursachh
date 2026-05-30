import json
import shutil
from pathlib import Path

from sqlalchemy import func, select

from app.db.models import Company
from app.services.company_source_discovery import (
    DISCOVERY_DISCLAIMER,
    CompanySourceDiscoveryService,
)


class FakeMoexIdentityClient:
    def __init__(self, rows=None, exc: Exception | None = None):
        self.rows = rows or []
        self.exc = exc
        self.called = False

    def search_securities(self, query: str, limit: int = 10):
        self.called = True
        if self.exc:
            raise self.exc
        return self.rows[:limit]


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class HeadFallbackClient:
    def __init__(self, timeout=15, follow_redirects=True):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def head(self, url):
        self.calls.append(("HEAD", url))
        return FakeResponse(405)

    def get(self, url, headers=None):
        self.calls.append(("GET", url, headers))
        return FakeResponse(200)


class FakeSessionLocal:
    def __init__(self, db):
        self.db = db

    def __call__(self):
        return self

    def __enter__(self):
        return self.db

    def __exit__(self, *_args):
        return None


def test_moex_identity_is_identity_only_not_financial_source(db_session):
    service = CompanySourceDiscoveryService(
        db_session,
        moex_client=FakeMoexIdentityClient(
            [
                {
                    "ticker": "TEST",
                    "isin": "RU000TEST",
                    "board": "TQBR",
                    "name": "Test Issuer",
                }
            ]
        ),
    )

    report = service.discover("unknown test issuer", limit=5)

    moex_items = [item for item in report.recommended_candidates if item.source_provider == "moex_iss"]
    assert moex_items
    assert moex_items[0].verification_scope == "identity_only"
    assert moex_items[0].trust_status == "trusted_candidate"
    assert "not a source of financial statements" in " ".join(moex_items[0].warnings)


def test_trusted_disclosure_candidates_are_source_page_metadata(db_session):
    service = CompanySourceDiscoveryService(db_session, moex_client=FakeMoexIdentityClient([]))

    report = service.discover("Татнефть", ticker="TATN")

    disclosure_items = [
        item
        for item in report.recommended_candidates
        if item.source_provider in {"e-disclosure", "disclosure_skrin"}
    ]
    assert disclosure_items
    assert all(item.verification_scope == "source_page_metadata" for item in disclosure_items)
    assert all(item.trust_status == "trusted_candidate" for item in disclosure_items)
    assert all(item.source_domain != "disclosure.ru" for item in disclosure_items)


def test_verified_live_does_not_imply_financial_document_verification(db_session):
    service = CompanySourceDiscoveryService(
        db_session,
        moex_client=FakeMoexIdentityClient([]),
        http_client_factory=HeadFallbackClient,
    )

    report = service.discover("LKOH", live=True)

    live_items = [item for item in report.recommended_candidates if item.verified_live]
    assert live_items
    assert all(item.verification_scope != "financial_document" for item in live_items)
    assert all(item.trust_status == "trusted_candidate" for item in live_items)


def test_head_fallback_to_get_metadata(db_session):
    client_instances = []

    class TrackingHeadFallbackClient(HeadFallbackClient):
        def __init__(self, timeout=15, follow_redirects=True):
            super().__init__(timeout=timeout, follow_redirects=follow_redirects)
            client_instances.append(self)

    service = CompanySourceDiscoveryService(
        db_session,
        moex_client=FakeMoexIdentityClient([]),
        http_client_factory=TrackingHeadFallbackClient,
    )

    report = service.discover("LKOH", live=True)

    assert any(candidate.verified_live for candidate in report.recommended_candidates)
    assert any(call[0] == "HEAD" for client in client_instances for call in client.calls)
    assert any(call[0] == "GET" for client in client_instances for call in client.calls)


def test_ambiguous_candidates_disable_auto_select(db_session):
    db_session.add(
        Company(
            ticker="LKOHX",
            board="TQBR",
            short_name="Lukoil extra",
            full_name="Lukoil extra issuer",
            aliases_json=["lkoh"],
            is_active=True,
        )
    )
    db_session.commit()
    service = CompanySourceDiscoveryService(db_session, moex_client=FakeMoexIdentityClient([]))

    report = service.discover("lkoh")

    assert report.auto_select_allowed is False
    assert any(candidate.trust_status == "ambiguous" for candidate in report.recommended_candidates)


def test_non_trusted_domain_rejected(db_session):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    company.ir_url = "https://example.com/investors"
    db_session.commit()
    service = CompanySourceDiscoveryService(db_session, moex_client=FakeMoexIdentityClient([]))

    report = service.discover("LKOH")

    assert not any(candidate.source_domain == "example.com" for candidate in report.recommended_candidates)


def test_cli_writes_json_report_with_disclaimer(monkeypatch, db_session):
    from app.tools import discover_company_sources as tool

    monkeypatch.setattr(tool, "SessionLocal", FakeSessionLocal(db_session))
    service_root = Path("tests/company_source_discovery_cli_test")
    if service_root.exists():
        shutil.rmtree(service_root)

    class TestService(CompanySourceDiscoveryService):
        def __init__(self, db):
            super().__init__(db, root=service_root, moex_client=FakeMoexIdentityClient([]))

    monkeypatch.setattr(tool, "CompanySourceDiscoveryService", TestService)

    assert tool.main(["LKOH", "--limit", "5"]) == 0

    path = service_root / "data" / "validation" / "company_source_discovery" / "lkoh_source_discovery_report.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["disclaimer"] == DISCOVERY_DISCLAIMER
    assert "best_candidate" not in payload
    assert isinstance(payload["recommended_candidates"], list)
    shutil.rmtree(service_root)


def test_discovery_does_not_mutate_company_or_manifests(db_session):
    manifest_path = Path("data/manifests/lkoh_real_sources.yml")
    manifest_before = manifest_path.read_text(encoding="utf-8")
    company_count_before = db_session.scalar(select(func.count()).select_from(Company))
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    ir_url_before = company.ir_url
    disclosure_id_before = company.disclosure_id

    service = CompanySourceDiscoveryService(db_session, moex_client=FakeMoexIdentityClient([]))
    service.discover("LKOH", live=False)

    db_session.expire(company)
    assert db_session.scalar(select(func.count()).select_from(Company)) == company_count_before
    assert company.ir_url == ir_url_before
    assert company.disclosure_id == disclosure_id_before
    assert manifest_path.read_text(encoding="utf-8") == manifest_before
