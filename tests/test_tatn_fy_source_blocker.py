from pathlib import Path
from uuid import uuid4

import yaml

from app.tools.compare_real_peers import compare
from app.tools.verify_lkoh_source_package import SourcePackageVerifier


def temp_root() -> Path:
    root = Path("data") / "validation" / "test_tatn_source_package" / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_manifest(root: Path, reports: list[dict]):
    path = root / "data" / "manifests"
    path.mkdir(parents=True)
    (path / "tatn_real_sources.yml").write_text(
        yaml.safe_dump({"company": "TATN", "reports": reports}, sort_keys=False),
        encoding="utf-8",
    )


def source(period: str, role: str = "financial_statements") -> dict:
    return {
        "period": period,
        "reporting_standard": "IFRS",
        "document_type": "financial_statement",
        "source_role": role,
        "source_url": "https://old.tatneft.ru/report.pdf",
        "expected_file_type": "pdf",
    }


def test_tatn_package_remains_partial_when_fy_source_missing():
    root = temp_root()
    write_manifest(root, [source("2021Q1"), source("2021Q2"), source("2021Q3")])

    report = SourcePackageVerifier("TATN", root=root).verify("2021Q1", "2021Q4")

    assert report["status"] == "PARTIAL"
    assert report["unresolved_blocker"] == "TATN FY/12M 2021 official IFRS source not verified"
    assert "2021Q4" in report["summary"]["missing_periods"]


def test_tatn_package_ready_only_when_fy_source_verified_in_manifest():
    root = temp_root()
    write_manifest(root, [source("2021Q1"), source("2021Q2"), source("2021Q3"), source("2021Q4")])

    report = SourcePackageVerifier("TATN", root=root).verify("2021Q1", "2021Q4")

    assert report["status"] == "READY"
    assert "unresolved_blocker" not in report


def test_peer_report_full_year_false_when_fy_missing():
    report = compare("LKOH", "TATN", "2021Q1", "2021Q4")

    assert report["full_year_comparison_available"] is False
    assert report["data_quality_by_company"]["TATN"]["fixture_data_used"] is False
