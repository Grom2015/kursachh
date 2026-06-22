import json
from pathlib import Path
from urllib.parse import quote

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


def test_artifact_endpoint_serves_validation_json(client):
    path = Path("data/validation/LKOH/2021Q4_manual_report_ingestion.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": "ok"}), encoding="utf-8")

    response = client.get("/reports/artifact/data/validation/LKOH/2021Q4_manual_report_ingestion.json")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_artifact_endpoint_serves_machine_report_json(client):
    path = Path("data/validation/LKOH/2021Q4_machine_report.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"machine_report_schema_version": "1.0"}), encoding="utf-8")

    response = client.get("/reports/artifact/data/validation/LKOH/2021Q4_machine_report.json")

    assert response.status_code == 200
    assert response.json()["machine_report_schema_version"] == "1.0"


def test_artifact_endpoint_serves_absolute_path_inside_allowed_roots(client):
    path = Path("data/validation/LKOH/2021Q4_llm_memo.md").resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# memo", encoding="utf-8")

    response = client.get(f"/reports/artifact/{quote(path.as_posix(), safe=':/')}")

    assert response.status_code == 200
    assert "# memo" in response.text


def test_artifact_endpoint_rejects_path_escape(client):
    response = client.get("/reports/artifact/../secrets.txt")
    assert response.status_code in {400, 404}
