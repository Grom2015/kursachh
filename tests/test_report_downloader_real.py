import httpx
import pytest

from app.core.config import get_settings
from app.db.models import Company
from app.services.reports.downloader import ReportDownloader, ReportDownloadError


class FakeResponse:
    def __init__(self, content=b"%PDF-1.4", headers=None, error=None):
        self.content = content
        self.headers = headers or {"content-type": "application/pdf"}
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise self._error


def _company(db_session):
    return db_session.get(Company, 1)


def test_download_mocked_pdf_response(monkeypatch, db_session):
    settings = get_settings()
    settings.report_source_allowed_domains = ["www.lukoil.ru"]
    monkeypatch.setattr(httpx, "get", lambda url, timeout, verify=True: FakeResponse())
    doc = ReportDownloader(db_session).download(
        _company(db_session),
        {
            "period": "2021Q1",
            "reporting_standard": "IFRS",
            "document_type": "financial_statement",
            "source_type": "issuer_ir_manifest",
            "source_url": "https://www.lukoil.ru/report.pdf",
            "expected_file_type": "pdf",
        },
    )
    assert doc.status == "downloaded"
    assert doc.file_hash
    assert "data\\raw" in doc.storage_path or "data/raw" in doc.storage_path
    assert doc.file_name.endswith(".pdf")


def test_download_adds_extension_when_url_has_no_suffix(monkeypatch, db_session):
    settings = get_settings()
    settings.report_source_allowed_domains = ["www.lukoil.com"]
    monkeypatch.setattr(httpx, "get", lambda url, timeout, verify=True: FakeResponse())
    doc = ReportDownloader(db_session).download(
        _company(db_session),
        {
            "period": "2021Q1",
            "reporting_standard": "IFRS",
            "document_type": "press_release",
            "source_type": "issuer_ir_manifest",
            "source_url": "https://www.lukoil.com/api/presscenter/exportpressrelease?id=546552",
            "expected_file_type": "pdf",
        },
    )
    assert doc.status == "downloaded"
    assert doc.file_name.endswith("_exportpressrelease.pdf")


def test_reject_oversized_file(monkeypatch, db_session):
    settings = get_settings()
    settings.report_source_allowed_domains = ["www.lukoil.ru"]
    settings.max_report_download_mb = 1
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, timeout, verify=True: FakeResponse(
            content=b"x", headers={"content-type": "application/pdf", "content-length": "2097152"}
        ),
    )
    with pytest.raises(ReportDownloadError, match="exceeds"):
        ReportDownloader(db_session).download(
            _company(db_session),
            {
                "period": "2021Q1",
                "reporting_standard": "IFRS",
                "source_type": "issuer_ir_manifest",
                "source_url": "https://www.lukoil.ru/big.pdf",
                "expected_file_type": "pdf",
            },
        )


def test_reject_non_allowlisted_domain(db_session):
    get_settings().report_source_allowed_domains = ["www.lukoil.ru"]
    with pytest.raises(ReportDownloadError, match="allowlisted"):
        ReportDownloader(db_session).download(
            _company(db_session),
            {
                "period": "2021Q1",
                "reporting_standard": "IFRS",
                "source_type": "issuer_ir_manifest",
                "source_url": "https://evil.example/report.pdf",
                "expected_file_type": "pdf",
            },
        )


def test_reject_fixture_path_traversal(db_session):
    with pytest.raises(ValueError, match="escapes data directory"):
        ReportDownloader(db_session).register(
            _company(db_session),
            {
                "period": "2021Q1",
                "reporting_standard": "IFRS",
                "source_type": "fixture",
                "fixture_path": "../README.md",
            },
        )


def test_deduplicate_by_source_url(monkeypatch, db_session):
    get_settings().report_source_allowed_domains = ["www.lukoil.ru"]
    monkeypatch.setattr(httpx, "get", lambda url, timeout, verify=True: FakeResponse())
    item = {
        "period": "2021Q1",
        "reporting_standard": "IFRS",
        "source_type": "issuer_ir_manifest",
        "source_url": "https://www.lukoil.ru/dedupe.pdf",
        "expected_file_type": "pdf",
    }
    first = ReportDownloader(db_session).download(_company(db_session), item)
    second = ReportDownloader(db_session).download(_company(db_session), item)
    assert first.id == second.id
