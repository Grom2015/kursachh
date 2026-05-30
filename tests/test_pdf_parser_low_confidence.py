from app.db.models import ReportDocument
from app.services.parsing.lkoh_ifrs_pdf_parser import LKOHIFRSPDFParser


def test_lkoh_parser_extracts_low_confidence_only_from_text(db_session):
    doc = ReportDocument(
        company_id=1,
        report_period="2021Q1",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_type="issuer_ir_manifest",
        source_url="https://www.lukoil.ru/report.pdf",
        storage_path="report.pdf",
    )
    db_session.add(doc)
    db_session.flush()
    facts = LKOHIFRSPDFParser()._extract_from_text(doc, "Revenue RUB million 12345", 3, 1)
    assert len(facts) == 1
    assert facts[0].metric_code == "revenue"
    assert facts[0].quality_flag == "low_confidence_parse"
    assert facts[0].confidence_score < 0.7


def test_lkoh_parser_does_not_invent_values(db_session):
    doc = ReportDocument(
        company_id=1,
        report_period="2021Q1",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_type="issuer_ir_manifest",
        source_url="https://www.lukoil.ru/report.pdf",
        storage_path="report.pdf",
    )
    db_session.add(doc)
    db_session.flush()
    facts = LKOHIFRSPDFParser()._extract_from_text(doc, "Revenue was disclosed in the report", 3, 1)
    assert facts == []

