from __future__ import annotations

from pathlib import Path

import httpx

from app.services.reports.moex_top50_catalog_checker import CatalogDownloadCheckRequest, MoexTop50CatalogChecker


class FakeClient:
    def __init__(self, responses: dict[str, httpx.Response], *args, **kwargs):
        self.responses = responses

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url: str, **kwargs):
        response = self.responses[url]
        response.request = httpx.Request("GET", url)
        return response


def _write_catalog(root: Path) -> None:
    reference = root / "data" / "reference"
    reference.mkdir(parents=True)
    header = (
        "rank,ticker,company_name,sector,issuer_website,investor_relations_url,reports_url,"
        "edisclosure_url,reporting_standards,document_types,suitable_for_analysis,source_trust_level,"
        "last_verified_at,verification_status,notes"
    )
    row = (
        "1,TEST,Test Issuer,oil,https://issuer.test,https://issuer.test/ir,https://issuer.test/reports,"
        "https://disclosure.skrin.ru/issuers?search=TEST,both,financial_statements,true,official issuer,"
        "2026-05-12,needs_manual_review,test"
    )
    (reference / "moex_top50_report_sources.csv").write_text(
        "\n".join([header, row]) + "\n",
        encoding="utf-8",
    )


def test_catalog_checker_discovers_downloads_and_validates_candidate(tmp_path: Path) -> None:
    _write_catalog(tmp_path)
    pdf = b"%PDF-1.4\nTest Issuer consolidated financial statements IFRS year ended 31 december 2021\n%%EOF"
    responses = {
        "https://issuer.test/reports": httpx.Response(
            200,
            text='<a href="/files/test-ifrs-consolidated-financial-statements-fy-2021.pdf">'
            "IFRS consolidated financial statements FY 2021</a>",
        ),
        "https://issuer.test/files/test-ifrs-consolidated-financial-statements-fy-2021.pdf": httpx.Response(
            200,
            content=pdf,
        ),
    }
    checker = MoexTop50CatalogChecker(
        root=tmp_path,
        http_client_factory=lambda *args, **kwargs: FakeClient(responses, *args, **kwargs),
    )

    report = checker.run(CatalogDownloadCheckRequest(period_from="2021Q1", period_to="2021Q4"))

    assert report["summary"]["pages_reachable_count"] == 1
    assert report["summary"]["report_candidates_found_count"] == 1
    assert report["summary"]["documents_downloaded_count"] == 1
    assert report["summary"]["source_status_counts"]["direct_report_links_found"] == 1
    assert report["company_checks"][0]["source_statuses"]["documents_downloaded"] is True
    assert report["company_checks"][0]["source_statuses"]["requires_manual_upload"] is False
    assert report["company_checks"][0]["downloaded_documents"][0]["storage_path"].startswith(
        "data/raw/catalog_sources/TEST/IFRS/2021Q4/"
    )
    assert report["safety"]["facts_persisted"] is False
    assert report["safety"]["metric_engine_invoked"] is False
    assert report["safety"]["manifests_mutated"] is False


def test_catalog_checker_no_download_mode_does_not_write_raw_files(tmp_path: Path) -> None:
    _write_catalog(tmp_path)
    responses = {
        "https://issuer.test/reports": httpx.Response(
            200,
            text='<a href="/files/test-ifrs-consolidated-financial-statements-fy-2021.pdf">'
            "IFRS consolidated financial statements FY 2021</a>",
        ),
    }
    checker = MoexTop50CatalogChecker(
        root=tmp_path,
        http_client_factory=lambda *args, **kwargs: FakeClient(responses, *args, **kwargs),
    )

    report = checker.run(
        CatalogDownloadCheckRequest(period_from="2021Q1", period_to="2021Q4", download=False)
    )

    assert report["summary"]["report_candidates_found_count"] == 1
    assert report["summary"]["documents_downloaded_count"] == 0
    assert not (tmp_path / "data" / "raw" / "catalog_sources").exists()


def test_catalog_checker_reports_blocker_when_page_fetch_fails(tmp_path: Path) -> None:
    _write_catalog(tmp_path)
    responses = {"https://issuer.test/reports": httpx.Response(404)}
    checker = MoexTop50CatalogChecker(
        root=tmp_path,
        http_client_factory=lambda *args, **kwargs: FakeClient(responses, *args, **kwargs),
    )

    report = checker.run(CatalogDownloadCheckRequest(period_from="2021Q1", period_to="2021Q4"))

    assert report["summary"]["blocked_count"] == 1
    assert report["company_checks"][0]["status"] == "BLOCKED"
    assert "reports_page_fetch_failed" in report["company_checks"][0]["reason"]
    assert "requires_disclosure_provider" in report["company_checks"][0]["acquisition_blockers"]


def test_catalog_checker_finds_embedded_json_report_links(tmp_path: Path) -> None:
    _write_catalog(tmp_path)
    responses = {
        "https://issuer.test/reports": httpx.Response(
            200,
            text=(
                '<script type="application/json">'
                '{"url":"\\/files\\/test-ifrs-consolidated-financial-statements-fy-2021.pdf"}'
                "</script>"
            ),
        ),
    }
    checker = MoexTop50CatalogChecker(
        root=tmp_path,
        http_client_factory=lambda *args, **kwargs: FakeClient(responses, *args, **kwargs),
    )

    report = checker.run(
        CatalogDownloadCheckRequest(period_from="2021Q1", period_to="2021Q4", download=False)
    )

    assert report["summary"]["report_candidates_found_count"] == 1
    assert report["company_checks"][0]["candidates"][0]["source_url"].endswith(
        "/files/test-ifrs-consolidated-financial-statements-fy-2021.pdf"
    )


def test_catalog_checker_classifies_russian_ifrs_links(tmp_path: Path) -> None:
    _write_catalog(tmp_path)
    responses = {
        "https://issuer.test/reports": httpx.Response(
            200,
            text='<a href="/files/msfo-2021.pdf">Консолидированная финансовая отчетность МСФО 2021</a>',
        ),
    }
    checker = MoexTop50CatalogChecker(
        root=tmp_path,
        http_client_factory=lambda *args, **kwargs: FakeClient(responses, *args, **kwargs),
    )

    report = checker.run(
        CatalogDownloadCheckRequest(period_from="2021Q1", period_to="2021Q4", download=False)
    )

    assert report["summary"]["direct_report_links_found_count"] == 1
    assert report["company_checks"][0]["candidates"][0]["source_role"] == "financial_statements"


def test_catalog_checker_keeps_annual_report_separate_from_financial_statements(tmp_path: Path) -> None:
    _write_catalog(tmp_path)
    responses = {
        "https://issuer.test/reports": httpx.Response(
            200,
            text='<a href="/files/annual-report-2021.pdf">Annual report 2021</a>',
        ),
    }
    checker = MoexTop50CatalogChecker(
        root=tmp_path,
        http_client_factory=lambda *args, **kwargs: FakeClient(responses, *args, **kwargs),
    )

    report = checker.run(
        CatalogDownloadCheckRequest(period_from="2021Q1", period_to="2021Q4", download=False)
    )

    assert report["summary"]["report_candidates_found_count"] == 1
    assert report["company_checks"][0]["status"] == "PARTIAL"
    assert report["summary"]["direct_report_links_found_count"] == 0
    assert report["company_checks"][0]["candidates"][0]["source_role"] == "annual_report"


def test_catalog_checker_records_rejected_candidate_samples(tmp_path: Path) -> None:
    _write_catalog(tmp_path)
    responses = {
        "https://issuer.test/reports": httpx.Response(
            200,
            text='<a href="/files/presentation-2021.pdf">Presentation 2021</a>',
        ),
    }
    checker = MoexTop50CatalogChecker(
        root=tmp_path,
        http_client_factory=lambda *args, **kwargs: FakeClient(responses, *args, **kwargs),
    )

    report = checker.run(
        CatalogDownloadCheckRequest(period_from="2021Q1", period_to="2021Q4", download=False)
    )

    assert report["summary"]["report_candidates_found_count"] == 0
    assert report["company_checks"][0]["source_statuses"]["requires_js_adapter"] is True
    assert report["company_checks"][0]["rejected_candidate_samples"][0]["rejection_reason"] == (
        "presentation_not_financial_statements"
    )


def test_catalog_checker_detects_current_year_quarter_and_fy_links(tmp_path: Path) -> None:
    _write_catalog(tmp_path)
    responses = {
        "https://issuer.test/reports": httpx.Response(
            200,
            text=(
                '<a href="/files/ifrs-consolidated-financial-statements-2025q1.pdf">'
                "IFRS consolidated financial statements 2025Q1</a>"
                '<a href="/files/ifrs-consolidated-financial-statements-fy-2025.pdf">'
                "IFRS consolidated financial statements FY 2025</a>"
            ),
        ),
    }
    checker = MoexTop50CatalogChecker(
        root=tmp_path,
        http_client_factory=lambda *args, **kwargs: FakeClient(responses, *args, **kwargs),
    )

    report = checker.run(
        CatalogDownloadCheckRequest(period_from="2025Q1", period_to="2025Q4", download=False)
    )

    periods = {candidate["period"] for candidate in report["company_checks"][0]["candidates"]}
    assert periods == {"2025Q1", "2025Q4"}
