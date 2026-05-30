from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from app.tools.compare_real_peers import compare
from app.tools.verify_lkoh_source_package import SourcePackageVerifier
from app.tools.verify_source_candidate import verify_candidate


def temp_root() -> Path:
    root = Path("data") / "validation" / "test_tatn_fy_resolution" / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_manifest(root: Path, reports: list[dict]) -> None:
    path = root / "data" / "manifests"
    path.mkdir(parents=True)
    (path / "tatn_real_sources.yml").write_text(
        yaml.safe_dump({"company": "TATN", "reports": reports}, sort_keys=False),
        encoding="utf-8",
    )


def source(period: str, url: str = "https://old.tatneft.ru/report.pdf") -> dict:
    return {
        "period": period,
        "reporting_standard": "IFRS",
        "document_type": "financial_statement",
        "source_role": "financial_statements",
        "source_url": url,
        "expected_file_type": "pdf",
    }


def test_candidate_official_fy_pdf_accepted_if_markers_present(monkeypatch):
    def fake_fetch(_url: str):
        return {
            "content": b"%PDF" + (b"x" * 60_000),
            "content_type": "application/pdf",
        }

    monkeypatch.setattr("app.tools.verify_source_candidate.fetch_candidate", fake_fetch)
    monkeypatch.setattr(
        "app.tools.verify_source_candidate.extract_first_pages_text",
        lambda _content: (
            "PJSC Tatneft Group IFRS consolidated financial statements 2021 "
            "year ended 31 December 2021 audited"
        ),
    )

    report = verify_candidate("https://old.tatneft.ru/storage/block_editor/files/fy2021.pdf", "TATN")

    assert report["status"] == "PASS"
    assert report["trusted_domain"] is True


def test_candidate_from_non_allowlisted_domain_rejected():
    report = verify_candidate("https://example.com/tatn-fy-2021.pdf", "TATN")

    assert report["status"] == "FAIL"
    assert report["failure_reason"] == "non_allowlisted_domain"


def test_source_package_ready_only_after_fy_verified():
    root = temp_root()
    write_manifest(root, [source("2021Q1"), source("2021Q2"), source("2021Q3"), source("2021Q4")])

    report = SourcePackageVerifier("TATN", root=root).verify("2021Q1", "2021Q4")

    assert report["status"] == "READY"
    assert report["summary"]["periods_with_financial_statements"] == 4


def test_source_package_remains_partial_if_fy_missing():
    root = temp_root()
    write_manifest(root, [source("2021Q1"), source("2021Q2"), source("2021Q3")])

    report = SourcePackageVerifier("TATN", root=root).verify("2021Q1", "2021Q4")

    assert report["status"] == "PARTIAL"
    assert report["unresolved_blocker"] == "TATN FY/12M 2021 official IFRS source not verified"


def test_no_fixture_fallback_in_tatn_real_source_package():
    root = temp_root()
    write_manifest(root, [source("2021Q1")])

    report = SourcePackageVerifier("TATN", root=root).verify("2021Q1", "2021Q4")

    assert report["status"] == "PARTIAL"
    assert all("fixture" not in str(period).casefold() for period in report["periods"])


def test_new_fy_checks_remain_pending_not_auto_verified():
    check = {
        "origin": "review_pack_sync",
        "period": "2021Q4",
        "metric_code": "revenue",
        "actual_extracted_value": 1.0,
        "expected_value": None,
        "manual_status": "pending",
    }

    assert check["manual_status"] == "pending"
    assert check["expected_value"] is None


@pytest.mark.parametrize("peer", ["TATN"])
def test_peer_full_year_available_only_when_fy_source_and_metrics_compatible(peer):
    report = compare("LKOH", peer, "2021Q1", "2021Q4")

    assert report["full_year_comparison_available"] is False
    assert report["analytical_readiness"] != "ready"
