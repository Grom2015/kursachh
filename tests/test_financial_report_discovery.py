from pathlib import Path

from sqlalchemy import func, select

from app.db.models import Company, ReportDocument
from app.services.reports.financial_report_discovery import (
    FinancialReportDiscoveryRequest,
    FinancialReportDiscoveryService,
    classify_report_link,
)


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")


class FakeClient:
    def __init__(self, timeout=15, follow_redirects=True):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def head(self, _url):
        return FakeResponse(status_code=405)

    def get(self, _url):
        return FakeResponse(
            '<a href="/ifrs_q1.pdf">IFRS consolidated financial statements 3 months 2021</a>'
            '<a href="/annual.pdf">Annual report 2021</a>'
        )


class EDisclosureFakeClient:
    def __init__(self, timeout=15, follow_redirects=True):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def head(self, _url):
        return FakeResponse(status_code=405)

    def get(self, url):
        if "files.aspx?id=347&type=4" in url:
            html = (
                '<a href="/portal/FileLoad.ashx?Fileid=777">'
                "Gazprom Neft IFRS consolidated financial statements 12 months 2025"
                "</a>"
            )
            return FakeResponse(html)
        return FakeResponse("")


def test_discovery_finds_ifrs_pdf_from_mocked_trusted_source_page(db_session):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    company.ir_url = "https://www.lukoil.com/reports"
    db_session.commit()
    service = FinancialReportDiscoveryService(db_session, http_client_factory=FakeClient)

    report = service.discover(
        FinancialReportDiscoveryRequest(
            company_query="LKOH",
            ticker="LKOH",
            period_from="2021Q1",
            period_to="2021Q1",
            reporting_standard="IFRS",
            live=True,
        )
    )

    assert report.status == "READY"
    assert any(item.source_url.endswith("/ifrs_q1.pdf") for item in report.discovered_reports)


def test_discovery_only_does_not_create_report_documents_or_mutate_manifests(db_session):
    before_count = db_session.scalar(select(func.count()).select_from(ReportDocument))
    manifest_path = Path("data/manifests/lkoh_real_sources.yml")
    before_manifest = manifest_path.read_text(encoding="utf-8")
    service = FinancialReportDiscoveryService(db_session)

    report = service.discover(
        FinancialReportDiscoveryRequest(
            company_query="LKOH",
            ticker="LKOH",
            period_from="2021Q1",
            period_to="2021Q4",
            reporting_standard="IFRS",
        )
    )

    assert report.status == "READY"
    assert db_session.scalar(select(func.count()).select_from(ReportDocument)) == before_count
    assert manifest_path.read_text(encoding="utf-8") == before_manifest


def test_discovery_rejects_ras_when_ifrs_requested():
    role, reason = classify_report_link("RAS accounting statements 2021", "IFRS")

    assert role == "unknown"
    assert reason == "required_financial_statement_markers_absent"


def test_discovery_rejects_annual_report_as_financial_statements():
    role, reason = classify_report_link("Annual report 2021", "IFRS")

    assert role == "annual_report"
    assert reason == "annual_report_not_validated_as_financial_statements"


def test_discovery_can_use_catalog_edisclosure_id_for_ifrs_files_page(db_session):
    company = db_session.scalar(select(Company).where(Company.ticker == "SIBN"))
    company.disclosure_id = None
    db_session.commit()
    service = FinancialReportDiscoveryService(db_session, http_client_factory=EDisclosureFakeClient)

    report = service.discover(
        FinancialReportDiscoveryRequest(
            company_query="SIBN",
            ticker="SIBN",
            period_from="2025Q4",
            period_to="2025Q4",
            reporting_standard="IFRS",
            live=True,
        )
    )

    assert report.status == "READY"
    assert any(
        item.source_page_url == "https://e-disclosure.ru/portal/files.aspx?id=347&type=4"
        for item in report.discovered_reports
    )
    assert any("FileLoad.ashx?Fileid=777" in item.source_url for item in report.discovered_reports)
