from app.db.models import ReportDocument
from app.services.parsing.audit import audit_path, write_parse_audit


def test_documents_endpoint(client, db_session):
    doc = ReportDocument(
        company_id=1,
        report_period="2021Q1",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_type="issuer_ir_manifest",
        source_url="https://www.lukoil.ru/report.pdf",
        file_name="report.pdf",
        file_hash="abc",
        status="downloaded",
    )
    db_session.add(doc)
    db_session.commit()
    response = client.get(
        "/reports/documents",
        params={"company": "LKOH", "period_from": "2021Q1", "period_to": "2021Q4", "data_mode": "real"},
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["source_type"] == "issuer_ir_manifest"


def test_parse_audit_endpoint(client, db_session):
    doc = ReportDocument(
        company_id=1,
        report_period="2021Q1",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_type="issuer_ir_manifest",
        source_url="https://www.lukoil.ru/report.pdf",
        status="parsed",
    )
    db_session.add(doc)
    db_session.flush()
    write_parse_audit(doc, "TestParser", 1, [], ["warning"])
    db_session.commit()
    response = client.get(f"/reports/documents/{doc.id}/parse-audit")
    assert response.status_code == 200
    assert response.json()["parser"] == "TestParser"


def test_parse_audit_404(client, db_session):
    doc = ReportDocument(
        company_id=1,
        report_period="2021Q1",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_type="issuer_ir_manifest",
        source_url="https://www.lukoil.ru/report.pdf",
        status="parsed",
    )
    db_session.add(doc)
    db_session.commit()
    path = audit_path(doc)
    if path.exists():
        path.unlink()
    response = client.get(f"/reports/documents/{doc.id}/parse-audit")
    assert response.status_code == 404
