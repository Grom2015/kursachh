import json
import uuid
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from app.db.models import Company, MetricValue, ReportDocument, StatementFact
from app.services.parsing.dataframe_fact_comparison import compare_dataframe_facts_to_existing
from app.services.parsing.statement_table_extractor import statement_tables_path
from app.tools import compare_dataframe_facts_to_existing as cli


class SessionContext:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *_args):
        return None


def _company(db_session):
    return db_session.scalar(select(Company).where(Company.ticker == "LKOH"))


def _doc(db_session, *, source_type="issuer_ir_manifest", source_role="financial_statements", period="2021Q4"):
    company = _company(db_session)
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period=period,
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role=source_role,
        source_type=source_type,
        source_url=f"https://www.lukoil.com/{period}-{uuid.uuid4().hex}.pdf",
        file_hash=f"hash-{period}-{source_type}-{uuid.uuid4().hex}",
        status="downloaded",
    )
    db_session.add(doc)
    db_session.flush()
    return doc


def _existing_fact(
    db_session,
    metric_code="revenue",
    value=100.0,
    unit_multiplier=1_000_000.0,
    period_type="annual",
    quality_flag="exact",
    doc=None,
):
    company = _company(db_session)
    doc = doc or _doc(db_session)
    fact = StatementFact(
        company_id=company.id,
        report_document_id=doc.id,
        period=doc.report_period,
        reporting_standard="IFRS",
        statement_type="income_statement",
        metric_code=metric_code,
        metric_name_original=metric_code,
        value=value,
        currency="RUB",
        unit_multiplier=unit_multiplier,
        period_type=period_type,
        source_location={"extraction_method": "issuer_specific_parser", "raw_label": metric_code},
        quality_flag=quality_flag,
        confidence_score=0.95,
    )
    db_session.add(fact)
    db_session.flush()
    _write_artifact(doc, [])
    return fact


def _artifact(db_session, rows, *, period="2021Q4", statement_type="income_statement", extraction_method="pdf_table"):
    doc = _doc(db_session, source_type="financial_report_discovery", period=period)
    table = {
        "document_id": doc.id,
        "company_ticker": "LKOH",
        "period": period,
        "reporting_standard": "IFRS",
        "statement_type": statement_type,
        "period_type": "annual",
        "table_index": 0,
        "page_number": 1,
        "table_title": statement_type,
        "unit": "million",
        "currency": "RUB",
        "columns": ["line", "2021"],
        "rows": rows,
        "dataframe_json": {"orientation": "records", "data": rows},
        "source_location": {"page": 1, "table_index": 0},
        "confidence_score": 0.85,
        "extraction_method": extraction_method,
        "quality_flag": "raw_table",
        "warnings": [],
    }
    _write_artifact(doc, [table])
    return doc


def _fallback_artifact(db_session, *, period="2021Q4"):
    rows = [
        {"line": "Consolidated Statement of Profit or Loss and Other Comprehensive Income"},
        {"line": "Note 2021 2020"},
        {"line": "Revenue 100 90"},
    ]
    doc = _doc(db_session, source_type="financial_report_discovery", period=period)
    table = {
        "document_id": doc.id,
        "company_ticker": "LKOH",
        "period": period,
        "reporting_standard": "IFRS",
        "statement_type": "income_statement",
        "table_index": 0,
        "page_number": 1,
        "table_title": "Consolidated Statement of Profit or Loss and Other Comprehensive Income",
        "unit": "million",
        "currency": "RUB",
        "columns": ["line"],
        "rows": rows,
        "dataframe_json": {"orientation": "records", "data": rows},
        "source_location": {"page": 1, "table_index": 0, "extraction_method": "text_table_fallback"},
        "confidence_score": 0.65,
        "extraction_method": "text_table_fallback",
        "quality_flag": "raw_text_table",
        "warnings": ["text_table_fallback_used"],
    }
    _write_artifact(doc, [table])
    return doc


def _write_artifact(doc, tables):
    path = statement_tables_path(doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"document_id": doc.id, "period": doc.report_period, "statement_tables": tables}),
        encoding="utf-8",
    )


def test_same_economic_value_counts_as_matched_same_and_needs_review_for_fallback(db_session):
    _existing_fact(db_session, value=100.0, unit_multiplier=1_000_000.0)
    _fallback_artifact(db_session)

    report = compare_dataframe_facts_to_existing(
        db_session,
        "LKOH",
        "2021Q4",
        "2021Q4",
        allow_text_fallback_semantic_gate=True,
    )

    assert report["matched_count"] == 1
    assert report["same_value_count"] == 1
    assert report["safe_to_persist_in_this_stage"] is False
    assert report["persist_eligibility"] == "needs_review"
    assert report["proposed_persist_plan"][0]["action"] in {"add_as_corroborating_source", "needs_review"}


def test_missing_existing_fact_reported(db_session):
    _artifact(db_session, [{"line": "Revenue", "2021": "100"}])

    report = compare_dataframe_facts_to_existing(db_session, "LKOH", "2021Q4", "2021Q4")

    assert report["existing_facts_count"] == 0
    assert report["missing_in_existing_count"] == 1
    assert report["proposed_persist_plan"][0]["action"] == "add_missing_fact_candidate"


def test_missing_dataframe_candidate_reported(db_session):
    _existing_fact(db_session, metric_code="total_assets", value=500.0, period_type="balance_sheet_snapshot")
    _artifact(db_session, [{"line": "Revenue", "2021": "100"}])

    report = compare_dataframe_facts_to_existing(db_session, "LKOH", "2021Q4", "2021Q4")

    assert report["missing_in_dataframe_count"] == 1
    assert report["missing_in_dataframe"][0]["metric_code"] == "total_assets"


def test_no_dataframe_candidates_requires_review_not_eligible(db_session):
    _existing_fact(db_session, metric_code="total_assets", value=500.0, period_type="balance_sheet_snapshot")

    report = compare_dataframe_facts_to_existing(db_session, "LKOH", "2021Q4", "2021Q4")

    assert report["dataframe_candidates_count"] == 0
    assert report["safe_to_persist_in_this_stage"] is False
    assert report["persist_eligibility"] == "needs_review"


def test_value_conflict_blocks_eligibility(db_session):
    _existing_fact(db_session, value=101.0, unit_multiplier=1_000_000.0)
    _artifact(db_session, [{"line": "Revenue", "2021": "100"}])

    report = compare_dataframe_facts_to_existing(db_session, "LKOH", "2021Q4", "2021Q4")

    assert report["persist_eligibility"] == "blocked"
    assert any(conflict["reason"] == "value_conflict" for conflict in report["conflicts"])


def test_period_type_conflict_blocks_eligibility(db_session):
    _existing_fact(db_session, value=100.0, unit_multiplier=1_000_000.0, period_type="ytd")
    _artifact(db_session, [{"line": "Revenue", "2021": "100"}])

    report = compare_dataframe_facts_to_existing(db_session, "LKOH", "2021Q4", "2021Q4")

    assert report["persist_eligibility"] == "blocked"
    assert any(conflict["reason"] == "period_type_conflict" for conflict in report["conflicts"])


def test_unit_multiplier_conflict_blocks_eligibility(db_session):
    _existing_fact(db_session, value=100_000_000.0, unit_multiplier=1.0)
    _artifact(db_session, [{"line": "Revenue", "2021": "100"}])

    report = compare_dataframe_facts_to_existing(db_session, "LKOH", "2021Q4", "2021Q4")

    assert report["persist_eligibility"] == "blocked"
    assert any(conflict["reason"] == "unit_multiplier_conflict" for conflict in report["conflicts"])


def test_fixture_existing_facts_excluded(db_session):
    fixture_doc = _doc(db_session, source_type="fixture")
    _existing_fact(db_session, value=100.0, quality_flag="fixture", doc=fixture_doc)
    _artifact(db_session, [{"line": "Revenue", "2021": "100"}])

    report = compare_dataframe_facts_to_existing(db_session, "LKOH", "2021Q4", "2021Q4")

    assert report["existing_facts_count"] == 0
    assert report["missing_in_existing_count"] == 1


def test_text_fallback_candidate_never_overrides_existing_high_confidence_fact(db_session):
    _existing_fact(db_session, value=100.0, unit_multiplier=1_000_000.0, quality_flag="high_confidence")
    _fallback_artifact(db_session)

    report = compare_dataframe_facts_to_existing(
        db_session,
        "LKOH",
        "2021Q4",
        "2021Q4",
        allow_text_fallback_semantic_gate=True,
    )

    assert report["safe_to_persist_in_this_stage"] is False
    assert report["proposed_persist_plan"][0]["action"] in {"add_as_corroborating_source", "needs_review"}
    assert report["proposed_persist_plan"][0]["action"] != "skip_existing_match"


def test_cli_writes_report_without_db_mutation_or_metric_invocation(db_session, monkeypatch):
    _existing_fact(db_session, value=100.0, unit_multiplier=1_000_000.0)
    _artifact(db_session, [{"line": "Revenue", "2021": "100"}])
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "SessionLocal", lambda: SessionContext(db_session))
    runtime_root = Path("tests/runtime_dataframe_fact_comparison")
    runtime_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(root_dir=runtime_root))
    before_facts = db_session.query(StatementFact).count()
    before_metrics = db_session.query(MetricValue).count()

    report = cli.run_comparison("LKOH", "2021Q4", "2021Q4", replay_cache=True)

    path = runtime_root / "data" / "validation" / "LKOH" / "2021Q4_2021Q4_dataframe_vs_existing_fact_comparison.json"
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["safe_to_persist_in_this_stage"] is False
    assert report["safe_to_persist_in_this_stage"] is False
    assert db_session.query(StatementFact).count() == before_facts
    assert db_session.query(MetricValue).count() == before_metrics
