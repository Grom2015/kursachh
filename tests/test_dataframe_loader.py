import json

import pandas as pd
import pytest
from sqlalchemy import select

from app.db.models import Company, ReportDocument
from app.services.parsing.dataframe_loader import (
    load_statement_table_as_dataframe,
    load_statement_tables_as_dataframes,
)
from app.services.parsing.statement_table_extractor import statement_tables_path


def test_dataframe_loader_reads_statement_table_artifact(db_session):
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
    path = statement_tables_path(doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "document_id": doc.id,
                "statement_tables": [
                    {
                        "statement_type": "balance_sheet",
                        "dataframe_json": {
                            "orientation": "records",
                            "data": [{"label": "Total assets", "2021": "100"}],
                        },
                    },
                    {
                        "statement_type": "income_statement",
                        "dataframe_json": {
                            "orientation": "records",
                            "data": [{"label": "Revenue", "2021": "200"}],
                        },
                    },
                    {
                        "statement_type": "unknown",
                        "dataframe_json": {
                            "orientation": "records",
                            "data": [{"label": "Diagnostic", "2021": "1"}],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    frames = load_statement_tables_as_dataframes(doc.id)
    balance_sheet = load_statement_table_as_dataframe(doc.id, "balance_sheet")
    income_statement = load_statement_table_as_dataframe(doc.id, "income_statement")

    assert isinstance(frames["balance_sheet"], pd.DataFrame)
    assert isinstance(frames["income_statement"], pd.DataFrame)
    assert isinstance(frames["unknown"], pd.DataFrame)
    assert not balance_sheet.empty
    assert not income_statement.empty
    assert list(balance_sheet.columns)
    assert balance_sheet.iloc[0]["label"] == "Total assets"
    assert income_statement.iloc[0]["label"] == "Revenue"


def test_dataframe_loader_raises_for_missing_statement_type(db_session):
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
    path = statement_tables_path(doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"document_id": doc.id, "statement_tables": []}), encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        load_statement_table_as_dataframe(doc.id, "cash_flow")


def test_dataframe_loader_raises_for_missing_artifact():
    with pytest.raises(FileNotFoundError):
        load_statement_tables_as_dataframes(999999)
