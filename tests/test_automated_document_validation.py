from pathlib import Path
from types import SimpleNamespace

import app.services.reports.document_validator as validator_module
from app.db.models import Company, ReportDocument
from app.services.reports.document_validator import DocumentValidator


def _runtime_file(name: str, content: bytes) -> str:
    root = Path("tests/runtime_auto_quality")
    root.mkdir(exist_ok=True)
    path = root / name
    path.write_bytes(content)
    return str(path)


def _document(company, role="financial_statements", content=None):
    storage = _runtime_file(
        "doc.pdf",
        content
        or (
            b"%PDF\nPJSC Test IFRS consolidated financial statements "
            b"for the year ended 31 December 2021 independent auditor report"
        ),
    )
    return ReportDocument(
        company=company,
        company_id=company.id or 1,
        report_period="2021Q4",
        reporting_standard="IFRS",
        document_type=role,
        source_role=role,
        source_type="issuer_ir_manifest",
        source_url="https://trusted.example/doc.pdf",
        storage_path=storage,
        file_name="doc.pdf",
        file_hash="abc",
        status="parsed",
    )


def test_official_ifrs_pdf_with_required_markers_passes_document_validation():
    company = Company(ticker="TEST", board="TQBR", short_name="PJSC Test", full_name="PJSC Test")
    result = DocumentValidator({"trusted.example"}).validate(_document(company))

    assert result.validation_status == "pass"
    assert result.detected_reporting_standard == "IFRS"
    assert result.detected_document_role == "financial_statements"


def test_annual_report_is_not_treated_as_ifrs_financial_statements():
    company = Company(ticker="TEST", board="TQBR", short_name="PJSC Test", full_name="PJSC Test")
    document = _document(company, content=b"%PDF\nPJSC Test annual report 2021")

    result = DocumentValidator({"trusted.example"}).validate(document)

    assert result.detected_document_role == "annual_report"
    assert result.validation_status in {"fail", "partial"}


def test_ras_statements_are_not_treated_as_ifrs_consolidated_statements():
    company = Company(ticker="TEST", board="TQBR", short_name="PJSC Test", full_name="PJSC Test")
    document = _document(company, content=b"%PDF\nPJSC Test RAS accounting statements 2021")

    result = DocumentValidator({"trusted.example"}).validate(document)

    assert result.detected_reporting_standard == "RAS"
    assert result.validation_status in {"fail", "partial"}


def test_extended_pdf_scan_depth_can_find_late_financial_statement_markers(monkeypatch):
    company = Company(ticker="TEST", board="TQBR", short_name="PJSC Test", full_name="PJSC Test")
    document = _document(company, content=b"%PDF\nplaceholder")

    class FakePdf:
        def __enter__(self):
            pages = [SimpleNamespace(extract_text=lambda: "") for _ in range(4)]
            pages.append(
                SimpleNamespace(
                    extract_text=lambda: (
                        "PJSC Test IFRS consolidated financial statements "
                        "statement of financial position for the year ended 31 December 2021"
                    )
                )
            )
            self.pages = pages
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(validator_module.pdfplumber, "open", lambda _path: FakePdf())

    default_result = DocumentValidator({"trusted.example"}).validate(document)
    extended_result = DocumentValidator({"trusted.example"}).validate(document, max_text_pages=5)

    assert default_result.validation_status in {"fail", "partial"}
    assert default_result.marker_results["ifrs_marker"] is False
    assert default_result.validator_text_pages_scanned == 3
    assert extended_result.validation_status == "pass"
    assert extended_result.marker_results["ifrs_marker"] is True
    assert extended_result.marker_results["primary_statements_marker"] is True
    assert extended_result.validator_text_pages_scanned == 5


def test_company_marker_uses_registry_aliases():
    company = Company(
        ticker="LKOH",
        board="TQBR",
        short_name="ЛУКОЙЛ",
        full_name='ПАО "ЛУКОЙЛ"',
        aliases_json=["lukoil"],
    )
    document = _document(
        company,
        content=(
            b"%PDF\nPJSC LUKOIL IFRS consolidated financial statements "
            b"statement of financial position for the year ended 31 December 2021"
        ),
    )

    result = DocumentValidator({"trusted.example"}).validate(document)

    assert result.marker_results["company_marker"] is True
    assert result.validation_status == "pass"


def test_company_marker_accepts_normalized_legal_name_variants():
    company = Company(
        ticker="MOEX",
        board="TQBR",
        short_name="МосБиржа",
        full_name="ПАО Московская Биржа",
        aliases_json=["MOEX"],
    )
    document = _document(
        company,
        content=(
            "%PDF\n"
            "ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО «МОСКОВСКАЯ БИРЖА ММВБ-РТС» "
            "IFRS consolidated financial statements statement of financial position "
            "for the year ended 31 December 2021 independent auditor report"
        ).encode(),
    )

    result = DocumentValidator({"trusted.example"}).validate(document)

    assert result.marker_results["company_marker"] is True
    assert result.validation_status == "pass"


def test_summarized_russian_consolidated_ifrs_statements_pass_validation():
    company = Company(
        ticker="SBER",
        board="TQBR",
        short_name="Сбербанк",
        full_name='Публичное акционерное общество "Сбербанк России"',
        aliases_json=['Публичное акционерное общество «Сбербанк России»'],
    )
    document = _document(
        company,
        content=(
            "%PDF\n"
            "Обобщенная консолидированная финансовая отчетность "
            "Публичное акционерное общество «Сбербанк России» и его дочерние организации "
            "за 2025 год с аудиторским заключением независимого аудитора. "
            "Обобщенный консолидированный отчет о финансовом положении. "
            "Подготовлена в соответствии со стандартами финансовой отчетности МСФО."
        ).encode(),
    )
    document.report_period = "2025"

    result = DocumentValidator({"trusted.example"}).validate(document)

    assert result.validation_status == "pass"
    assert result.detected_reporting_standard == "IFRS"
    assert result.detected_document_role == "financial_statements"
    assert result.marker_results["ifrs_marker"] is True
    assert result.marker_results["consolidated_financial_statements_marker"] is True
    assert result.marker_results["auditor_marker"] is True
