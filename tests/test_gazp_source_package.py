from pathlib import Path
from uuid import uuid4

import yaml

from app.tools.verify_lkoh_source_package import SourcePackageVerifier


def temp_root() -> Path:
    root = Path("data") / "validation" / "test_gazp_source_package" / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_manifest(root: Path, reports: list[dict]) -> None:
    path = root / "data" / "manifests"
    path.mkdir(parents=True)
    (path / "gazp_real_sources.yml").write_text(
        yaml.safe_dump({"company": "GAZP", "reports": reports}, sort_keys=False),
        encoding="utf-8",
    )


def source(period: str, url: str = "https://www.gazprom.com/report.pdf") -> dict:
    return {
        "period": period,
        "reporting_standard": "IFRS",
        "document_type": "financial_statement",
        "source_role": "financial_statements",
        "source_url": url,
        "expected_file_type": "pdf",
    }


def test_gazp_manifest_with_all_four_proper_sources_is_ready():
    root = temp_root()
    write_manifest(root, [source("2021Q1"), source("2021Q2"), source("2021Q3"), source("2021Q4")])

    report = SourcePackageVerifier("GAZP", root=root).verify("2021Q1", "2021Q4")

    assert report["status"] == "READY"
    assert report["summary"]["periods_with_financial_statements"] == 4
    assert report["summary"]["missing_periods"] == []


def test_gazp_manifest_missing_fy_is_partial():
    root = temp_root()
    write_manifest(root, [source("2021Q1"), source("2021Q2"), source("2021Q3")])

    report = SourcePackageVerifier("GAZP", root=root).verify("2021Q1", "2021Q4")

    assert report["status"] == "PARTIAL"
    assert report["summary"]["periods_with_financial_statements"] == 3
    assert report["summary"]["missing_periods"] == ["2021Q4"]


def test_gazp_non_allowlisted_domain_is_rejected_with_warning():
    root = temp_root()
    write_manifest(root, [source("2021Q1", url="https://untrusted.example.com/report.pdf")])

    report = SourcePackageVerifier("GAZP", root=root).verify("2021Q1", "2021Q1")

    assert report["status"] == "READY"
    assert report["periods"][0]["sources"][0]["trusted_domain"] is False
    assert any("not allowlisted" in warning for warning in report["warnings"])


def test_gazp_live_warning_prevents_ready_status(monkeypatch):
    root = temp_root()
    write_manifest(root, [source("2021Q1"), source("2021Q2"), source("2021Q3"), source("2021Q4")])
    monkeypatch.setattr(
        SourcePackageVerifier,
        "_live_content_type",
        lambda self, url: (None, "Live metadata check failed: network unavailable", "live_request_failed"),
    )

    report = SourcePackageVerifier("GAZP", root=root).verify("2021Q1", "2021Q4", live=True)

    assert report["status"] == "NOT_READY"
    assert report["summary"]["periods_with_verified_financial_statements"] == 0
    assert all(not source["source_verified"] for period in report["periods"] for source in period["sources"])
