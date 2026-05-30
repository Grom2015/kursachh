from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.core.config import get_settings
from app.db.models import Company
from app.services.reports.downloader import ReportDownloader, ReportDownloadError


class FakeResponse:
    def __init__(self, content=b"%PDF-1.4", headers=None):
        self.content = content
        self.headers = headers or {"content-type": "application/pdf"}

    def raise_for_status(self):
        return None


def _company(db_session):
    return db_session.get(Company, 1)


def _item(url="https://www.gazprom.com/report.pdf"):
    return {
        "period": "2021Q1",
        "reporting_standard": "IFRS",
        "document_type": "financial_statement",
        "source_type": "issuer_ir_manifest",
        "source_url": url,
        "expected_file_type": "pdf",
    }


def test_downloader_default_verify_mode_uses_true_not_false(monkeypatch, db_session):
    captured = {}
    get_settings().report_source_allowed_domains = ["www.gazprom.com"]
    get_settings().report_tls_ca_bundle = ""

    def fake_get(url, timeout, verify=True):
        captured["verify"] = verify
        return FakeResponse()

    monkeypatch.setattr(httpx, "get", fake_get)
    ReportDownloader(db_session).download(_company(db_session), _item())

    assert captured["verify"] is True
    assert captured["verify"] is not False


def test_downloader_custom_ca_bundle_path_is_passed(monkeypatch, db_session):
    captured = {}
    root = Path("data") / "validation" / "test_downloader_tls" / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    ca_path = root / "ca.pem"
    ca_path.write_text("test ca", encoding="utf-8")
    get_settings().report_source_allowed_domains = ["www.gazprom.com"]
    get_settings().report_tls_ca_bundle = str(ca_path)

    def fake_get(url, timeout, verify=True):
        captured["verify"] = verify
        return FakeResponse()

    monkeypatch.setattr(httpx, "get", fake_get)
    try:
        ReportDownloader(db_session).download(_company(db_session), _item(url="https://www.gazprom.com/custom-ca.pdf"))
    finally:
        get_settings().report_tls_ca_bundle = ""

    assert captured["verify"] == str(ca_path)


def test_downloader_missing_custom_ca_bundle_is_controlled(db_session):
    root = Path("data") / "validation" / "test_downloader_tls" / uuid4().hex
    get_settings().report_source_allowed_domains = ["www.gazprom.com"]
    get_settings().report_tls_ca_bundle = str(root / "missing.pem")
    try:
        with pytest.raises(ReportDownloadError, match="TLS configuration error"):
            ReportDownloader(db_session).download(_company(db_session), _item(url="https://www.gazprom.com/missing-ca.pdf"))
    finally:
        get_settings().report_tls_ca_bundle = ""


def test_downloader_empty_custom_ca_bundle_is_controlled(db_session):
    root = Path("data") / "validation" / "test_downloader_tls" / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    ca_path = root / "empty.pem"
    ca_path.write_text("", encoding="utf-8")
    get_settings().report_source_allowed_domains = ["www.gazprom.com"]
    get_settings().report_tls_ca_bundle = str(ca_path)
    try:
        with pytest.raises(ReportDownloadError, match="empty file"):
            ReportDownloader(db_session).download(_company(db_session), _item(url="https://www.gazprom.com/empty-ca.pdf"))
    finally:
        get_settings().report_tls_ca_bundle = ""


def test_downloader_tls_error_has_stable_failure_reason(monkeypatch, db_session):
    get_settings().report_source_allowed_domains = ["www.gazprom.com"]
    get_settings().report_tls_ca_bundle = ""

    def fake_get(url, timeout, verify=True):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(ReportDownloadError, match="tls_certificate_verify_failed"):
        ReportDownloader(db_session).download(_company(db_session), _item(url="https://www.gazprom.com/tls-fail.pdf"))
