import json

from sqlalchemy import select

from app.db.models import Company, ReportDocument, StatementFact
from app.services.parsing.statement_table_extractor import statement_tables_path
from app.tools import parse_statement_tables_to_facts as cli


class SessionContext:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *_args):
        return None


def _seed_artifact(db_session):
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
                        "company_ticker": "LKOH",
                        "period": "2021Q4",
                        "reporting_standard": "IFRS",
                        "statement_type": "income_statement",
                        "table_index": 0,
                        "unit": "million",
                        "currency": "RUB",
                        "rows": [{"line": "Revenue", "2021": "100"}],
                        "source_location": {"page": 1, "table_index": 0},
                        "extraction_method": "pdf_table",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return doc


def test_parse_statement_tables_report_only_does_not_mutate_db(db_session, monkeypatch):
    _seed_artifact(db_session)
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "SessionLocal", lambda: SessionContext(db_session))
    before = db_session.query(StatementFact).count()

    report = cli.parse_statement_tables_to_facts("LKOH", "2021Q1", "2021Q4", replay_cache=True, persist=False)

    assert report["canonical_facts_created"] == 1
    assert report["db_persisted"] is False
    assert db_session.query(StatementFact).count() == before


def test_parse_statement_tables_persist_inserts_dataframe_facts(db_session, monkeypatch):
    _seed_artifact(db_session)
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "SessionLocal", lambda: SessionContext(db_session))

    report = cli.parse_statement_tables_to_facts("LKOH", "2021Q1", "2021Q4", replay_cache=True, persist=True)

    fact = db_session.scalar(select(StatementFact).where(StatementFact.metric_code == "revenue"))
    assert report["persisted_facts_count"] == 1
    assert fact is not None
    assert fact.source_location["extraction_method"] == "dataframe_statement_parser"


def test_parse_statement_tables_does_not_overwrite_existing_issuer_fact(db_session, monkeypatch):
    doc = _seed_artifact(db_session)
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    existing = StatementFact(
        company_id=company.id,
        report_document_id=doc.id,
        period="2021Q4",
        reporting_standard="IFRS",
        statement_type="income_statement",
        metric_code="revenue",
        metric_name_original="Issuer parser revenue",
        value=999.0,
        source_location={"extraction_method": "issuer_specific_parser"},
        quality_flag="high_confidence",
    )
    db_session.add(existing)
    db_session.commit()
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "SessionLocal", lambda: SessionContext(db_session))

    cli.parse_statement_tables_to_facts("LKOH", "2021Q1", "2021Q4", replay_cache=True, persist=True)

    db_session.refresh(existing)
    assert existing.value == 999.0
