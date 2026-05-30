from app.db.models import Company, ReportDocument
from app.services.reports.financial_report_discovery import DiscoveredFinancialReport
from app.services.reports.report_download_validation import ReportDownloadValidationService


class FakeValidation:
    def __init__(self, status="pass", role="financial_statements"):
        self.validation_status = status
        self.detected_document_role = role
        self.detected_reporting_standard = "IFRS"
        self.marker_results = {
            "company_marker": True,
            "period_marker": True,
            "ifrs_marker": True,
            "ras_marker": False,
            "consolidated_financial_statements_marker": True,
            "primary_statements_marker": True,
            "notes_marker": True,
        }

    def to_dict(self):
        return {
            "validation_status": self.validation_status,
            "detected_document_role": self.detected_document_role,
            "detected_reporting_standard": self.detected_reporting_standard,
            "marker_results": self.marker_results,
        }


def _candidate(role="financial_statements"):
    return DiscoveredFinancialReport(
        company_ticker="LKOH",
        period="2021Q1",
        period_type="q1",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role=role,
        source_url="https://www.lukoil.com/report.pdf",
        source_domain="www.lukoil.com",
        source_page_url=None,
        title="IFRS consolidated financial statements",
        language="en",
        expected_file_type="pdf",
        confidence_score=0.9,
    )


def test_download_validation_rejected_doc_remains_audit_trail(db_session, monkeypatch):
    service = ReportDownloadValidationService(db_session)
    company = db_session.get(Company, 1)

    def fake_download(_company, item):
        doc = ReportDocument(
            company_id=_company.id,
            report_period=item["period"],
            reporting_standard="IFRS",
            document_type="financial_statement",
            source_role="financial_statements",
            source_type="financial_report_discovery",
            source_url=item["source_url"],
            storage_path="missing.pdf",
            file_name="missing.pdf",
            file_hash="hash",
            status="downloaded",
        )
        db_session.add(doc)
        db_session.flush()
        return doc

    monkeypatch.setattr(service.downloader, "download", fake_download)
    monkeypatch.setattr(service.validator, "validate", lambda *_args, **_kwargs: FakeValidation(status="partial"))

    report = service.download_and_validate(company, [_candidate()], "2021Q1", "2021Q1")

    doc = db_session.get(ReportDocument, report.items[0].document_id)
    assert doc.status == "rejected"
    assert doc.rejection_reason == "document_validation_partial"
    assert report.rejected_documents_count == 1


def test_annual_report_embedded_financial_statements_preserves_source_role(db_session, monkeypatch):
    service = ReportDownloadValidationService(db_session)
    company = db_session.get(Company, 1)

    def fake_download(_company, item):
        doc = ReportDocument(
            company_id=_company.id,
            report_period=item["period"],
            reporting_standard="IFRS",
            document_type="annual_report",
            source_role="annual_report",
            source_type="financial_report_discovery",
            source_url=item["source_url"],
            storage_path="missing.pdf",
            file_name="missing.pdf",
            file_hash="hash",
            status="downloaded",
        )
        db_session.add(doc)
        db_session.flush()
        return doc

    monkeypatch.setattr(service.downloader, "download", fake_download)
    monkeypatch.setattr(service.validator, "validate", lambda *_args, **_kwargs: FakeValidation())

    report = service.download_and_validate(company, [_candidate("annual_report")], "2021Q1", "2021Q1")

    doc = db_session.get(ReportDocument, report.items[0].document_id)
    assert doc.status == "downloaded"
    assert doc.source_role == "annual_report_with_embedded_financial_statements"


def test_annual_report_without_embedded_statements_is_rejected(db_session, monkeypatch):
    service = ReportDownloadValidationService(db_session)
    company = db_session.get(Company, 1)

    def fake_download(_company, item):
        doc = ReportDocument(
            company_id=_company.id,
            report_period=item["period"],
            reporting_standard="IFRS",
            document_type="annual_report",
            source_role="annual_report",
            source_type="financial_report_discovery",
            source_url=item["source_url"],
            storage_path="missing.pdf",
            file_name="missing.pdf",
            file_hash="hash",
            status="downloaded",
        )
        db_session.add(doc)
        db_session.flush()
        return doc

    monkeypatch.setattr(service.downloader, "download", fake_download)
    monkeypatch.setattr(
        service.validator,
        "validate",
        lambda *_args, **_kwargs: FakeValidation(status="pass", role="annual_report"),
    )

    report = service.download_and_validate(company, [_candidate("annual_report")], "2021Q1", "2021Q1")

    doc = db_session.get(ReportDocument, report.items[0].document_id)
    assert doc.status == "rejected"
    assert doc.rejection_reason == "embedded_financial_statements_not_proven"


def test_download_validation_accepts_financial_statement(db_session, monkeypatch):
    service = ReportDownloadValidationService(db_session)
    company = db_session.get(Company, 1)

    def fake_download(_company, item):
        doc = ReportDocument(
            company_id=_company.id,
            report_period=item["period"],
            reporting_standard="IFRS",
            document_type="financial_statement",
            source_role="financial_statements",
            source_type="financial_report_discovery",
            source_url=item["source_url"],
            storage_path="missing.pdf",
            file_name="missing.pdf",
            file_hash="hash",
            status="downloaded",
        )
        db_session.add(doc)
        db_session.flush()
        return doc

    monkeypatch.setattr(service.downloader, "download", fake_download)
    monkeypatch.setattr(service.validator, "validate", lambda *_args, **_kwargs: FakeValidation())

    report = service.download_and_validate(company, [_candidate()], "2021Q1", "2021Q1")

    assert report.downloaded_documents_count == 1
    assert report.validated_financial_statements_count == 1
    assert report.rejected_documents_count == 0
