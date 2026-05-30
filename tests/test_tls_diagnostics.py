from pathlib import Path
from uuid import uuid4

import httpx
import yaml

from app.core.config import get_settings
from app.services.reports import tls
from app.tools import diagnose_tls_source
from app.tools.verify_lkoh_source_package import SourcePackageVerifier


def temp_root() -> Path:
    root = Path("data") / "validation" / "test_tls_diagnostics" / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_manifest(root: Path, url: str = "https://www.gazprom.com/report.pdf") -> None:
    path = root / "data" / "manifests"
    path.mkdir(parents=True)
    (path / "gazp_real_sources.yml").write_text(
        yaml.safe_dump(
            {
                "company": "GAZP",
                "reports": [
                    {
                        "period": "2021Q1",
                        "reporting_standard": "IFRS",
                        "document_type": "financial_statement",
                        "source_role": "financial_statements",
                        "source_url": url,
                        "expected_file_type": "pdf",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


class TLSFailClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def head(self, _url):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")


class OKClient:
    captured_verify = None

    def __init__(self, **kwargs):
        OKClient.captured_verify = kwargs.get("verify")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def head(self, url):
        return httpx.Response(200, headers={"content-type": "application/pdf"}, request=httpx.Request("HEAD", url))


def test_default_verify_mode_reports_certifi_path():
    get_settings().report_tls_ca_bundle = ""

    fields = tls.tls_report_fields()

    assert fields["tls_verify_mode"] == "default"
    assert fields["ca_bundle_path_used"] is None
    assert fields["ca_bundle_exists"] is False
    assert fields["ca_bundle_size_bytes"] is None
    assert fields["certifi_path"]


def test_custom_ca_bundle_path_is_passed_to_source_verifier(monkeypatch):
    root = temp_root()
    ca_path = root / "ca.pem"
    ca_path.write_text("test ca", encoding="utf-8")
    write_manifest(root)
    get_settings().report_source_allowed_domains = ["www.gazprom.com"]
    get_settings().report_tls_ca_bundle = str(ca_path)
    monkeypatch.setattr("app.tools.verify_lkoh_source_package.httpx.Client", OKClient)
    try:
        report = SourcePackageVerifier("GAZP", root=root).verify("2021Q1", "2021Q1", live=True)
    finally:
        get_settings().report_tls_ca_bundle = ""

    assert OKClient.captured_verify == str(ca_path)
    assert report["tls_verify_mode"] == "custom_ca_bundle"
    assert report["ca_bundle_exists"] is True
    assert report["ca_bundle_size_bytes"] > 0
    assert report["status"] == "READY"


def test_missing_custom_ca_bundle_returns_controlled_config_error():
    root = temp_root()
    write_manifest(root)
    get_settings().report_tls_ca_bundle = str(root / "missing.pem")
    try:
        report = SourcePackageVerifier("GAZP", root=root).verify("2021Q1", "2021Q1", live=True)
    finally:
        get_settings().report_tls_ca_bundle = ""

    source = report["periods"][0]["sources"][0]
    assert report["status"] == "NOT_READY"
    assert source["failure_reason"] == "tls_config_error"
    assert report["ca_bundle_exists"] is False
    assert report["tls_config_error"]


def test_empty_custom_ca_bundle_returns_controlled_config_error():
    root = temp_root()
    ca_path = root / "empty.pem"
    ca_path.write_text("", encoding="utf-8")
    write_manifest(root)
    get_settings().report_tls_ca_bundle = str(ca_path)
    try:
        report = SourcePackageVerifier("GAZP", root=root).verify("2021Q1", "2021Q1", live=True)
    finally:
        get_settings().report_tls_ca_bundle = ""

    source = report["periods"][0]["sources"][0]
    assert report["status"] == "NOT_READY"
    assert source["failure_reason"] == "tls_config_error"
    assert report["ca_bundle_exists"] is True
    assert report["ca_bundle_size_bytes"] == 0
    assert "empty file" in report["tls_config_error"]


def test_tls_certificate_verify_error_maps_to_failure_reason(monkeypatch):
    root = temp_root()
    write_manifest(root)
    get_settings().report_tls_ca_bundle = ""
    monkeypatch.setattr("app.tools.verify_lkoh_source_package.httpx.Client", TLSFailClient)

    report = SourcePackageVerifier("GAZP", root=root).verify("2021Q1", "2021Q1", live=True)

    source = report["periods"][0]["sources"][0]
    assert source["failure_reason"] == "tls_certificate_verify_failed"
    assert source["source_verified"] is False


def test_tls_diagnostics_generates_report_payload(monkeypatch):
    get_settings().report_source_allowed_domains = ["www.gazprom.com"]
    get_settings().report_tls_ca_bundle = ""
    monkeypatch.setattr("app.tools.diagnose_tls_source.httpx.Client", TLSFailClient)
    monkeypatch.setattr("app.tools.diagnose_tls_source.certificate_metadata", lambda _url: {"subject": "test"})

    report = diagnose_tls_source.diagnose_url("https://www.gazprom.com/report.pdf")

    assert report["failure_reason"] == "tls_certificate_verify_failed"
    assert report["certificate"] == {"subject": "test"}


def test_source_package_tls_diagnostics_path_is_recorded(monkeypatch):
    root = temp_root()
    write_manifest(root)
    monkeypatch.setattr("app.tools.verify_lkoh_source_package.httpx.Client", TLSFailClient)
    monkeypatch.setattr(
        "app.tools.verify_lkoh_source_package.diagnose_url",
        lambda url: {"hostname": "www.gazprom.com", "url": url},
    )
    monkeypatch.setattr(
        "app.tools.verify_lkoh_source_package.save_tls_diagnostics_report",
        lambda report: Path("data/validation/TLS/gazprom_tls_diagnostics.json"),
    )

    report = SourcePackageVerifier("GAZP", root=root).verify("2021Q1", "2021Q1", live=True, tls_diagnostics=True)

    source = report["periods"][0]["sources"][0]
    assert source["tls_diagnostics_path"] == "data\\validation\\TLS\\gazprom_tls_diagnostics.json" or source[
        "tls_diagnostics_path"
    ] == "data/validation/TLS/gazprom_tls_diagnostics.json"
