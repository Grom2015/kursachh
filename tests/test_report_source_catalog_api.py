from fastapi.testclient import TestClient

from app.main import app
from app.services.reports.report_source_catalog import ReportSourceCatalog


def test_report_source_catalog_returns_top50_items() -> None:
    catalog = ReportSourceCatalog().all_sources()

    assert catalog["companies_count"] == 50
    assert len(catalog["items"]) == 50
    assert catalog["acquisition_policy"]["document_must_pass_validation"] is True


def test_report_source_for_ticker_includes_manual_upload_guidance() -> None:
    result = ReportSourceCatalog().source_for_ticker("LKOH")

    assert result["found"] is True
    assert result["item"]["ticker"] == "LKOH"
    assert result["manual_upload_guidance"]["trust_boundary"]["official_source_verified"] is False
    first_link = result["manual_upload_guidance"]["recommended_links"][0]
    assert first_link["label"] == "E-Disclosure company card"
    assert first_link["url"] == "https://e-disclosure.ru/portal/company.aspx?id=17"
    direct_urls = [link["url"] for link in result["manual_upload_guidance"]["recommended_links"]]
    assert "https://e-disclosure.ru/portal/files.aspx?id=17&type=4" in direct_urls
    assert 'ПАО "ЛУКОЙЛ"' in result["manual_upload_guidance"]["search_terms"]
    assert "LKOH" in result["manual_upload_guidance"]["search_terms"]


def test_report_source_for_irao_prefers_russian_edisclosure_search_terms() -> None:
    result = ReportSourceCatalog().source_for_ticker("IRAO")

    assert result["manual_upload_guidance"]["recommended_links"][0]["url"] == "https://e-disclosure.ru/poisk-po-kompaniyam"
    assert result["manual_upload_guidance"]["search_terms"][:2] == ['ПАО "Интер РАО"', "Интер РАО"]


def test_report_source_for_moex_includes_known_edisclosure_direct_links() -> None:
    result = ReportSourceCatalog().source_for_ticker("MOEX")

    assert result["found"] is True
    assert result["item"]["edisclosure_company_id"] == "43"
    direct_urls = [link["url"] for link in result["manual_upload_guidance"]["recommended_links"]]
    assert "https://e-disclosure.ru/portal/files.aspx?id=43&type=4" in direct_urls


def test_report_source_api_returns_catalog_and_ticker_links() -> None:
    client = TestClient(app)

    catalog_response = client.get("/report-sources/catalog")
    ticker_response = client.get("/report-sources/LKOH")

    assert catalog_response.status_code == 200
    assert catalog_response.json()["companies_count"] == 50
    assert ticker_response.status_code == 200
    assert ticker_response.json()["item"]["ticker"] == "LKOH"


def test_unknown_ticker_returns_404_with_manual_guidance() -> None:
    client = TestClient(app)

    response = client.get("/report-sources/UNKNOWN")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["manual_upload_guidance"]["recommended_action"] == "use_edisclosure_search_then_manual_upload"
    assert detail["manual_upload_guidance"]["recommended_links"][0]["label"] == "E-Disclosure company search"
    assert detail["manual_upload_guidance"]["recommended_links"][0]["url"] == "https://e-disclosure.ru/poisk-po-kompaniyam"
