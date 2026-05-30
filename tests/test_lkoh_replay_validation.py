import json
import uuid

from app.core.config import get_settings
from app.db.models import Company, StatementFact
from app.services.parsing.audit import audit_path, write_parse_audit
from app.services.reports.source_adapters import DiscoveredReport
from app.tools.audit_lkoh_real_metrics import run_audit_from_validation_json
from app.tools.replay_lkoh_parser import run_parser_replay
from app.tools.validate_lkoh_real_extraction import run_replay_cache_validation


def _cached_report(monkeypatch, period="2021Q1", source_role="financial_statements"):
    filename = f"pytest_{uuid.uuid4().hex}.pdf"
    raw_dir = get_settings().root_dir / "data" / "raw" / "LKOH" / "IFRS" / period
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"hash_{filename}"
    path.write_bytes(b"%PDF-1.4\n% pytest cached pdf\n")
    report = DiscoveredReport(
        company_ticker="LKOH",
        report_period=period,
        period_from=None,
        period_to=None,
        reporting_standard="IFRS",
        document_type="financial_statement" if source_role == "financial_statements" else "press_release",
        source_role=source_role,
        source_type="issuer_ir_manifest",
        source_url=f"https://www.lukoil.com/FileSystem/9/{filename}",
        title="pytest cached report",
        language="en",
        published_at=None,
        expected_file_type="pdf",
        confidence_score=0.9,
        notes=None,
    )
    monkeypatch.setattr(
        "app.tools.validate_lkoh_real_extraction.RealSourceManifest.load_for_company",
        lambda self, ticker, period_from, period_to, reporting_standard: [report],
    )
    return path


def _fake_parse(self, document):
    fact = StatementFact(
        company_id=document.company_id,
        report_document_id=document.id,
        period=document.report_period,
        reporting_standard=document.reporting_standard,
        statement_type="income_statement",
        metric_code="revenue",
        metric_name_original="Sales (including excise and export tariffs)",
        value=100.0,
        currency="RUB",
        unit_multiplier=1_000_000,
        period_type="ytd",
        source_location={
            "source_type": document.source_type,
            "source_role": document.source_role,
            "source_url": document.source_url,
            "document_id": document.id,
            "page": 5,
            "table": "statement_text",
            "line": 10,
            "raw_label": "Sales (including excise and export tariffs)",
        },
        quality_flag="exact",
        confidence_score=0.9,
    )
    write_parse_audit(document, self.__class__.__name__, 1, [fact], [])
    debug_path = audit_path(document).with_name(f"{document.id}_table_debug.json")
    debug_path.write_text(
        json.dumps(
            {
                "document_id": document.id,
                "period": document.report_period,
                "source_role": document.source_role,
                "tables_found": 1,
                "candidate_rows": [
                    {
                        "table_index": "statement_text",
                        "page": 5,
                        "raw_label": "Sales (including excise and export tariffs)",
                        "raw_values": ["100"],
                        "matched_metric_code": "revenue",
                        "match_reason": "exact_label_match",
                        "confidence_score": 0.9,
                        "warnings": [],
                    }
                ],
                "warnings": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return [fact]


def test_replay_cache_with_mocked_cached_documents_produces_validation_report(db_session, monkeypatch):
    path = _cached_report(monkeypatch)
    monkeypatch.setattr("app.tools.validate_lkoh_real_extraction.LKOHIFRSPDFParser.parse", _fake_parse)
    try:
        company = db_session.get(Company, 1)
        report = run_replay_cache_validation(db_session, company, "2021Q1", "2021Q1")
    finally:
        path.unlink(missing_ok=True)
    assert report["validation_mode"] == "replay_cache"
    assert report["input_documents_source"] == "cached_raw_files"
    assert report["summary"]["source_document_count"] == 1
    assert report["summary"]["facts_extracted_count"] == 1


def test_replay_cache_with_no_cached_docs_returns_controlled_fail(db_session, monkeypatch):
    report_item = DiscoveredReport(
        company_ticker="LKOH",
        report_period="2099Q1",
        period_from=None,
        period_to=None,
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="issuer_ir_manifest",
        source_url="https://www.lukoil.com/FileSystem/9/not_cached.pdf",
        title="missing",
        language="en",
        published_at=None,
        expected_file_type="pdf",
        confidence_score=0.9,
    )
    monkeypatch.setattr(
        "app.tools.validate_lkoh_real_extraction.RealSourceManifest.load_for_company",
        lambda self, ticker, period_from, period_to, reporting_standard: [report_item],
    )
    company = db_session.get(Company, 1)
    report = run_replay_cache_validation(db_session, company, "2099Q1", "2099Q1")
    assert report["status"] == "FAIL"
    assert "Cached real documents not found" in " ".join(report["warnings"])


def test_replay_cache_never_uses_fixture_data(db_session, monkeypatch):
    path = _cached_report(monkeypatch)
    monkeypatch.setattr("app.tools.validate_lkoh_real_extraction.LKOHIFRSPDFParser.parse", _fake_parse)
    try:
        company = db_session.get(Company, 1)
        report = run_replay_cache_validation(db_session, company, "2021Q1", "2021Q1")
    finally:
        path.unlink(missing_ok=True)
    assert report["fixture_data_used"] is False
    assert report["data_mode"] == "real"


def test_parser_only_replay_creates_table_debug_and_audit_artifacts(db_session, monkeypatch):
    path = _cached_report(monkeypatch)
    monkeypatch.setattr("app.tools.replay_lkoh_parser.LKOHIFRSPDFParser.parse", _fake_parse)
    try:
        report = run_parser_replay("2021Q1", "2021Q1", db=db_session)
    finally:
        path.unlink(missing_ok=True)
    assert report["documents_processed"] == 1
    assert report["tables_found"] == 1
    assert report["strong_matches_count"] == 1
    assert report["extracted_facts_count"] == 1


def test_metric_audit_warns_when_using_validation_report_fallback():
    period_from = "2097Q1"
    period_to = "2097Q1"
    root = get_settings().root_dir / "data" / "validation" / "LKOH"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{period_from}_{period_to}_real_validation_report.json"
    path.write_text(
        json.dumps(
            {
                "metric_coverage": [
                    {
                        "period": period_from,
                        "metric_code": "net_margin",
                        "value": 0.1,
                        "quality_flag": "exact",
                        "formula": "net_income / revenue",
                        "inputs": {"net_income": 10, "revenue": 100},
                        "warnings": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    try:
        report = run_audit_from_validation_json(period_from, period_to)
    finally:
        path.unlink(missing_ok=True)
    assert report["source"] == "validation_report_fallback"
    assert report["audit_input_freshness"] == "validation_report_fallback"
    assert any("Validation JSON fallback" in warning for warning in report["warnings"])


def test_metric_audit_marks_stale_or_missing_when_no_inputs():
    period_from = "2096Q1"
    period_to = "2096Q1"
    path = (
        get_settings().root_dir
        / "data"
        / "validation"
        / "LKOH"
        / f"{period_from}_{period_to}_real_validation_report.json"
    )
    path.unlink(missing_ok=True)
    report = run_audit_from_validation_json(period_from, period_to)
    assert report["audit_input_freshness"] == "stale_or_missing"
    assert report["metrics_total"] == 0
