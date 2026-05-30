import json
from contextlib import contextmanager

from sqlalchemy import select

from app.db.models import Company, ReportDocument, StatementFact
from app.services.parsing.statement_table_extractor import statement_tables_path
from app.tools import smoke_statement_table_pipeline as smoke


@contextmanager
def _session_context(session):
    yield session


def test_statement_table_pipeline_smoke_passes_minimum_without_facts_or_metrics(db_session, monkeypatch):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2021Q1",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="financial_report_discovery",
        source_url="https://www.lukoil.com/report.pdf",
        status="downloaded",
    )
    db_session.add(doc)
    db_session.flush()
    artifact = statement_tables_path(doc)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        json.dumps(
            {
                "document_id": doc.id,
                "statement_tables": [
                    {
                        "statement_type": "balance_sheet",
                        "dataframe_json": {"orientation": "records", "data": [{"label": "Total assets"}]},
                    },
                    {
                        "statement_type": "income_statement",
                        "dataframe_json": {"orientation": "records", "data": [{"label": "Revenue"}]},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    extraction = {
        "tables_extracted": 2,
        "statement_tables_count": 2,
        "balance_sheet_tables_count": 1,
        "income_statement_tables_count": 1,
        "cash_flow_tables_count": 0,
        "statement_coverage": {
            "balance_sheet": {"found": True, "count": 1, "periods": ["2021Q1"]},
            "income_statement": {"found": True, "count": 1, "periods": ["2021Q1"]},
            "cash_flow": {"found": False, "count": 0, "periods": []},
        },
        "facts_extracted": 0,
        "fact_parser_status": "not_invoked",
        "document_reports": [{"document_id": doc.id, "artifact_path": str(artifact), "statement_tables": []}],
        "warnings": [],
    }

    monkeypatch.setattr(smoke, "init_db", lambda: None)
    monkeypatch.setattr(smoke, "SessionLocal", lambda: _session_context(db_session))
    monkeypatch.setattr(smoke, "extract_statement_tables", lambda *_args, **_kwargs: extraction)

    before_facts = db_session.query(StatementFact).count()
    report = smoke.run_smoke("LKOH", "2021Q1", "2021Q4", "IFRS", replay_cache=True)

    assert report["task_acceptance_minimum_passed"] is True
    assert report["balance_sheet_dataframe_loaded"] is True
    assert report["income_statement_dataframe_loaded"] is True
    assert report["facts_extracted"] == 0
    assert report["fact_parser_status"] == "not_invoked"
    assert db_session.query(StatementFact).count() == before_facts


def test_statement_table_pipeline_smoke_requires_replay_cache():
    try:
        smoke.run_smoke("LKOH", "2021Q1", "2021Q4", "IFRS", replay_cache=False)
    except RuntimeError as exc:
        assert "replay-cache" in str(exc)
    else:
        raise AssertionError("Expected replay-cache guard to raise")
