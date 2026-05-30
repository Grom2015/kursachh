import json
import shutil
from pathlib import Path
from uuid import uuid4

import yaml

from app.db.models import Company, ReportDocument
from app.services.parsing.tatn_ifrs_pdf_parser import TATNIFRSPDFParser
from app.tools.compare_real_peers import compare, comparison_warnings
from app.tools.validate_real_extraction import empty_report, save_report
from app.tools.verify_lkoh_source_package import SourcePackageVerifier


def _root() -> Path:
    root = Path("data") / "validation" / "test_generic_validation_tools" / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_manifest(root: Path, ticker: str, reports: list[dict]) -> None:
    path = root / "data" / "manifests" / f"{ticker.casefold()}_real_sources.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"company": ticker, "source_type": "issuer_ir_manifest", "reports": reports}
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


def _source(period: str, role: str = "financial_statements") -> dict:
    return {
        "period": period,
        "reporting_standard": "IFRS",
        "document_type": "financial_statement",
        "source_role": role,
        "source_url": f"https://old.tatneft.ru/storage/block_editor/files/{period}.pdf",
        "expected_file_type": "pdf",
        "language": "en",
    }


def test_generic_source_package_verifier_works_for_ticker():
    root = _root()
    try:
        _write_manifest(root, "TATN", [_source("2021Q1")])
        report = SourcePackageVerifier("TATN", root=root).verify("2021Q1", "2021Q1")

        assert report["company"] == "TATN"
        assert report["status"] == "READY"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_tatn_manifest_only_press_release_is_not_ready():
    root = _root()
    try:
        _write_manifest(root, "TATN", [_source("2021Q1", role="press_release")])
        report = SourcePackageVerifier("TATN", root=root).verify("2021Q1", "2021Q1")

        assert report["status"] == "NOT_READY"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_tatn_manifest_all_periods_financial_statements_is_ready():
    root = _root()
    try:
        _write_manifest(root, "TATN", [_source("2021Q1"), _source("2021Q2"), _source("2021Q3"), _source("2021Q4")])
        report = SourcePackageVerifier("TATN", root=root).verify("2021Q1", "2021Q4")

        assert report["status"] == "READY"
        assert report["summary"]["periods_with_financial_statements"] == 4
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_tatn_q4_missing_keeps_source_package_partial():
    report = SourcePackageVerifier("TATN").verify("2021Q1", "2021Q4")

    assert report["status"] == "PARTIAL"
    assert report["summary"]["missing_periods"] == ["2021Q4"]


def test_tatn_q4_annual_report_can_make_package_ready():
    root = _root()
    try:
        _write_manifest(
            root,
            "TATN",
            [
                _source("2021Q1"),
                _source("2021Q2"),
                _source("2021Q3"),
                _source("2021Q4", role="annual_report"),
            ],
        )
        report = SourcePackageVerifier("TATN", root=root).verify("2021Q1", "2021Q4")

        assert report["status"] == "READY"
        assert report["summary"]["periods_with_annual_report"] == 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_tatn_parser_creates_high_confidence_fact_from_mocked_statement_text():
    company = Company(id=2, ticker="TATN", board="TQBR", short_name="TATN", full_name="TATN")
    document = ReportDocument(
        id=10,
        company_id=2,
        company=company,
        report_period="2021Q1",
        reporting_standard="IFRS",
        source_type="issuer_ir_manifest",
        source_role="financial_statements",
        source_url="https://old.tatneft.ru/report.pdf",
        storage_path="report.pdf",
    )
    parser = TATNIFRSPDFParser()
    text = "\n".join(
            [
                "TATNEFT",
                "Consolidated Interim Condensed Statement of Profit or Loss and Other Comprehensive Income",
                "Sales and other operating revenues on non-",
                "banking activities, net 250,000 200,000",
                "Profit for the period 55,000 40,000",
            ]
        )

    facts = parser._extract_from_statement_text(document, text, page_number=3)

    assert len(facts) == 2
    assert all(fact.confidence_score >= 0.8 for fact in facts)
    assert {fact.metric_code for fact in facts} == {"revenue", "net_income"}


def test_generic_validation_saves_report_under_ticker_directory():
    report = empty_report("TATN", "2099Q1", "2099Q4", "replay_cache", "no docs")
    path = save_report(report)

    assert "TATN" in str(path)
    assert json.loads(path.read_text(encoding="utf-8"))["company"] == "TATN"


def test_tatn_real_empty_report_never_uses_fixture_fallback():
    report = empty_report("TATN", "2021Q1", "2021Q4", "replay_cache", "no docs")

    assert report["fixture_data_used"] is False
    assert report["status"] == "FAIL"


def test_peer_comparison_marks_not_ready_when_peer_result_missing():
    report = compare("LKOH", "TATN", "2099Q1", "2099Q4")

    assert report["status"] == "NOT_READY"
    assert report["comparable_metrics_count"] == 0
    assert any("TATN real validation result not found" in warning for warning in report["warnings"])


def test_peer_comparison_status_partial_when_shared_metric_exists():
    warnings = comparison_warnings(
        "TATN",
        {"TATN": {"validation_status": "PARTIAL", "metrics_valid": 1, "missing_periods": ["2021Q4"]}},
        [{"metric_code": "net_margin", "comparison_status": "comparable"}],
        [{"metric_code": "pe_ratio", "period": "2021Q2", "reason": "valuation_inputs_missing"}],
    )

    assert "TATN Q4/FY source is missing." in warnings
    assert any("partial" in warning for warning in warnings)
