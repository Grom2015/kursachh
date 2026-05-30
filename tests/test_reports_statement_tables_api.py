import json

from sqlalchemy import select

from app.db.models import Company, ReportDocument
from app.services.parsing.statement_table_extractor import statement_tables_path


def test_reports_discover_api_returns_discovery_report(client):
    response = client.get(
        "/reports/discover",
        params={
            "company": "LKOH",
            "period_from": "2021Q1",
            "period_to": "2021Q4",
            "reporting_standard": "IFRS",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "READY"
    assert "does not mean documents are downloaded" in payload["disclaimer"]


def test_statement_tables_api_returns_artifact(client, db_session):
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
                        "statement_type": "cash_flow",
                        "dataframe_json": {"orientation": "records", "data": []},
                    }
                ],
                "warnings": [],
            }
        ),
        encoding="utf-8",
    )

    response = client.get(f"/reports/documents/{doc.id}/statement-tables")

    assert response.status_code == 200
    payload = response.json()
    assert payload["document_id"] == doc.id
    assert payload["items"][0]["statement_type"] == "cash_flow"
