import json
from contextlib import contextmanager

from sqlalchemy import select

from app.db.models import Company, ReportDocument, StatementFact
from app.services.parsing.statement_table_extractor import statement_tables_path
from app.tools import smoke_dataframe_fact_pipeline as smoke


@contextmanager
def _session_context(session):
    yield session


def test_dataframe_fact_pipeline_smoke_passes_minimum_without_persistence(db_session, monkeypatch):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2021Q4",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="financial_report_discovery",
        source_url="https://www.lukoil.com/report.pdf",
        status="downloaded",
    )
    db_session.add(doc)
    db_session.flush()
    path = statement_tables_path(doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "document_id": doc.id,
                "period": "2021Q4",
                "statement_tables": [
                    {
                        "period": "2021Q4",
                        "reporting_standard": "IFRS",
                        "statement_type": "income_statement",
                        "table_index": 0,
                        "unit": "million",
                        "currency": "RUB",
                        "rows": [{"line": "Revenue", "2021": "100"}, {"line": "Profit for the year", "2021": "10"}],
                        "source_location": {"page": 1, "table_index": 0},
                        "extraction_method": "pdf_table",
                    },
                    {
                        "period": "2021Q4",
                        "reporting_standard": "IFRS",
                        "statement_type": "balance_sheet",
                        "table_index": 1,
                        "unit": "million",
                        "currency": "RUB",
                        "rows": [{"line": "Total assets", "2021": "500"}, {"line": "Total equity", "2021": "250"}],
                        "source_location": {"page": 2, "table_index": 1},
                        "extraction_method": "pdf_table",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(smoke, "init_db", lambda: None)
    monkeypatch.setattr(smoke, "SessionLocal", lambda: _session_context(db_session))
    before = db_session.query(StatementFact).count()

    report = smoke.run_smoke("LKOH", "2021Q1", "2021Q4", "IFRS", replay_cache=True)

    assert report["task_minimum_fact_smoke_passed"] is True
    assert report["db_persisted"] is False
    assert db_session.query(StatementFact).count() == before


def test_dataframe_fact_pipeline_smoke_requires_replay_cache():
    try:
        smoke.run_smoke("LKOH", "2021Q1", "2021Q4", "IFRS", replay_cache=False)
    except RuntimeError as exc:
        assert "replay-cache" in str(exc)
    else:
        raise AssertionError("Expected replay-cache guard to raise")


def test_dataframe_fact_pipeline_smoke_accepts_explicit_text_fallback_gate(db_session, monkeypatch):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2021Q4",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="financial_report_discovery",
        source_url="https://www.lukoil.com/report.pdf",
        status="downloaded",
    )
    db_session.add(doc)
    db_session.flush()
    path = statement_tables_path(doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "document_id": doc.id,
                "period": "2021Q4",
                "statement_tables": [
                    {
                        "document_id": doc.id,
                        "period": "2021Q4",
                        "reporting_standard": "IFRS",
                        "statement_type": "income_statement",
                        "table_index": 0,
                        "page_number": 5,
                        "table_title": "Consolidated Statement of Profit or Loss and Other Comprehensive Income",
                        "unit": "million",
                        "currency": "RUB",
                        "rows": [
                            {"line": "Consolidated Statement of Profit or Loss and Other Comprehensive Income"},
                            {"line": "Note 2021 2020"},
                            {"line": "Revenue 100 90"},
                        ],
                        "source_location": {"page": 5, "table_index": 0},
                        "extraction_method": "text_table_fallback",
                        "quality_flag": "raw_text_table",
                    },
                    {
                        "document_id": doc.id,
                        "period": "2021Q4",
                        "reporting_standard": "IFRS",
                        "statement_type": "balance_sheet",
                        "table_index": 1,
                        "page_number": 7,
                        "table_title": "Consolidated Statement of Financial Position",
                        "unit": "million",
                        "currency": "RUB",
                        "rows": [
                            {"line": "Consolidated Statement of Financial Position"},
                            {"line": "Note 31 December 2021 31 December 2020"},
                            {"line": "Total assets 500 400"},
                        ],
                        "source_location": {"page": 7, "table_index": 1},
                        "extraction_method": "text_table_fallback",
                        "quality_flag": "raw_text_table",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(smoke, "init_db", lambda: None)
    monkeypatch.setattr(smoke, "SessionLocal", lambda: _session_context(db_session))

    report = smoke.run_smoke(
        "LKOH",
        "2021Q1",
        "2021Q4",
        "IFRS",
        replay_cache=True,
        allow_text_fallback_semantic_gate=True,
    )

    assert report["task_minimum_fact_smoke_passed"] is True
    assert report["text_fallback_semantic_gate_enabled"] is True
    assert report["db_persisted"] is False
