import sys
import types
from pathlib import Path
from zipfile import ZipFile

import pandas as pd
from sqlalchemy import select

from app.db.models import Company, ReportDocument
from app.services.parsing.statement_table_extractor import (
    StatementTableExtractor,
    classify_statement_table,
)


def _runtime_path(file_name: str) -> Path:
    root = Path("tests/runtime_statement_tables")
    root.mkdir(parents=True, exist_ok=True)
    return root / file_name


def _document(db_session, file_name: str, content: bytes | str, status: str = "downloaded") -> ReportDocument:
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    path = _runtime_path(file_name)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2021Q4",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="financial_report_discovery",
        source_url=f"https://www.lukoil.com/{file_name}",
        storage_path=str(path),
        file_name=file_name,
        file_hash="hash",
        status=status,
    )
    db_session.add(doc)
    db_session.flush()
    return doc


def test_html_extraction_creates_dataframe_compatible_json(db_session):
    html = """
    <table>
      <tr><th>Statement of financial position</th><th>2021</th></tr>
      <tr><td>Total assets</td><td>100</td></tr>
    </table>
    """
    doc = _document(db_session, "report.html", html)

    report = StatementTableExtractor().extract(doc)

    assert report["facts_extracted"] == 0
    assert report["fact_parser_status"] == "not_invoked"
    assert report["statement_tables_count"] == 1
    assert report["statement_tables"][0]["statement_type"] == "balance_sheet"
    assert report["statement_tables"][0]["extraction_method"] == "html_table"
    assert report["statement_tables"][0]["quality_flag"] == "raw_table"
    assert report["statement_tables"][0]["dataframe_json"]["orientation"] == "records"
    assert report["statement_coverage"]["balance_sheet"]["found"] is True


def test_xlsx_extraction_creates_statement_table(db_session):
    path = _runtime_path("report.xlsx")
    pd.DataFrame([["Statement of cash flows", "2021"], ["Net cash from operating activities", "10"]]).to_excel(
        path, index=False, header=False
    )
    doc = _document(db_session, "placeholder.html", "")
    doc.storage_path = str(path)
    doc.file_name = "report.xlsx"

    report = StatementTableExtractor().extract(doc)

    assert report["tables_extracted"] == 1
    assert report["statement_tables"][0]["statement_type"] == "cash_flow"
    assert report["statement_tables"][0]["extraction_method"] == "xlsx_table"


def test_pdf_extraction_uses_pdfplumber_when_available(db_session, monkeypatch):
    class FakePage:
        def extract_text(self):
            return "Statement of profit or loss"

        def extract_tables(self):
            return [[["Statement of profit or loss", "2021"], ["Revenue", "100"]]]

    class FakePdf:
        pages = [FakePage()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    fake_pdfplumber = types.SimpleNamespace(open=lambda _path: FakePdf())
    monkeypatch.setitem(sys.modules, "pdfplumber", fake_pdfplumber)
    doc = _document(db_session, "report.pdf", b"%PDF fake")

    report = StatementTableExtractor().extract(doc)

    assert report["statement_tables"][0]["statement_type"] == "income_statement"
    assert report["statement_tables"][0]["extraction_method"] == "pdf_table"
    assert report["statement_tables"][0]["statement_family"] == "income_statement"
    assert "row_blocks" in report["statement_tables"][0]


def test_zip_extraction_processes_supported_members(db_session):
    zip_path = _runtime_path("reports.zip")
    with ZipFile(zip_path, "w") as archive:
        archive.writestr(
            "cashflow.html",
            "<table><tr><th>Statement of cash flows</th><th>2021</th></tr><tr><td>Cash</td><td>1</td></tr></table>",
        )
    doc = _document(db_session, "placeholder.html", "")
    doc.storage_path = str(zip_path)
    doc.file_name = "reports.zip"

    report = StatementTableExtractor().extract(doc)

    assert report["tables_extracted"] == 1
    assert report["statement_tables"][0]["statement_type"] == "cash_flow"


def test_unknown_table_saved_as_diagnostic(db_session):
    html = "<table><tr><th>Random table</th><th>2021</th></tr><tr><td>Foo</td><td>1</td></tr></table>"
    doc = _document(db_session, "unknown.html", html)

    report = StatementTableExtractor().extract(doc)

    assert report["tables_extracted"] == 1
    assert report["statement_tables_count"] == 0
    assert report["statement_tables"][0]["statement_type"] == "unknown"
    assert report["statement_tables"][0]["warnings"]


def test_replay_extraction_ignores_rejected_documents(db_session):
    doc = _document(
        db_session,
        "rejected.html",
        "<table><tr><td>Statement of cash flows</td></tr></table>",
        status="rejected",
    )

    report = StatementTableExtractor().extract(doc)

    assert report["tables_extracted"] == 0
    assert "Document status is not cached/validated" in report["warnings"][0]


def test_ifrs_and_ras_statement_classification():
    ras_balance = (
        "\u0411\u0443\u0445\u0433\u0430\u043b\u0442\u0435\u0440\u0441\u043a\u0438\u0439 "
        "\u0431\u0430\u043b\u0430\u043d\u0441"
    )
    ras_income = (
        "\u041e\u0442\u0447\u0435\u0442 \u043e \u0444\u0438\u043d\u0430\u043d\u0441\u043e\u0432\u044b\u0445 "
        "\u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u0430\u0445"
    )
    ras_cash_flow = (
        "\u041e\u0442\u0447\u0435\u0442 \u043e \u0434\u0432\u0438\u0436\u0435\u043d\u0438\u0438 "
        "\u0434\u0435\u043d\u0435\u0436\u043d\u044b\u0445 \u0441\u0440\u0435\u0434\u0441\u0442\u0432"
    )
    assert classify_statement_table("Statement of financial position", "IFRS") == "balance_sheet"
    assert classify_statement_table("Statement of profit or loss", "IFRS") == "income_statement"
    assert classify_statement_table("Statement of cash flows", "IFRS") == "cash_flow"
    assert classify_statement_table("Обобщенный консолидированный отчет о финансовом положении", "IFRS") == "balance_sheet"
    assert classify_statement_table("Обобщенный консолидированный отчет о прибылях и убытках", "IFRS") == "income_statement"
    assert classify_statement_table("Обобщенный консолидированный отчет о движении денежных средств", "IFRS") == "cash_flow"
    assert classify_statement_table(ras_balance, "RAS") == "balance_sheet"
    assert classify_statement_table(ras_income, "RAS") == "income_statement"
    assert classify_statement_table(ras_cash_flow, "RAS") == "cash_flow"


def test_cash_flow_classification_requires_strong_markers():
    assert classify_statement_table("PJSC LUKOIL Consolidated Statement of Cash Flows", "IFRS") == "cash_flow"
    assert (
        classify_statement_table(
            "Cash flows from operating activities Net cash used in investing activities "
            "Net cash used in financing activities",
            "IFRS",
        )
        == "cash_flow"
    )
    assert classify_statement_table("Notes to financial statements Cash and cash equivalents", "IFRS") == "notes"


def test_pdf_text_fallback_only_for_primary_statement_pages(db_session, monkeypatch):
    class CashFlowPage:
        def extract_text(self):
            return "\n".join(
                [
                    "PJSC LUKOIL",
                    "Consolidated Statement of Cash Flows",
                    "Cash flows from operating activities",
                    "Net cash used in investing activities",
                    "Net cash used in financing activities",
                    "Cash and cash equivalents at end of period",
                ]
            )

        def extract_tables(self):
            return []

    class NotesPage:
        def extract_text(self):
            return "Notes to financial statements Cash and cash equivalents"

        def extract_tables(self):
            return []

    class FakePdf:
        pages = [CashFlowPage(), NotesPage()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setitem(sys.modules, "pdfplumber", types.SimpleNamespace(open=lambda _path: FakePdf()))
    doc = _document(db_session, "fallback.pdf", b"%PDF fake")

    report = StatementTableExtractor().extract(doc)

    assert report["tables_extracted"] == 1
    table = report["statement_tables"][0]
    assert table["statement_type"] == "cash_flow"
    assert table["extraction_method"] == "text_table_fallback"
    assert table["quality_flag"] == "raw_text_table"
    assert table["warnings"] == ["text_table_fallback_used"]
    assert report["statement_coverage"]["cash_flow"]["found"] is True


def test_primary_income_page_with_header_only_pdf_table_gets_text_fallback(db_session, monkeypatch):
    class IncomePage:
        def extract_text(self):
            return "\n".join(
                [
                    "PJSC LUKOIL",
                    "Consolidated Statement of Profit or Loss and Other Comprehensive Income",
                    "(Millions of Russian rubles)",
                    "Note 2021 2020",
                    "Sales and other operating revenues 2 285 161 1 934 320",
                    "Profit for the year 157 414 46 292",
                ]
            )

        def extract_tables(self):
            return [[["Current income taxes"], ["Deferred income taxes"]]]

    class FakePdf:
        pages = [IncomePage()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setitem(sys.modules, "pdfplumber", types.SimpleNamespace(open=lambda _path: FakePdf()))
    doc = _document(db_session, "income-fallback.pdf", b"%PDF fake")

    report = StatementTableExtractor().extract(doc)

    fallback_tables = [table for table in report["statement_tables"] if table["extraction_method"] == "text_table_fallback"]
    assert len(fallback_tables) == 1
    assert fallback_tables[0]["statement_type"] == "income_statement"
    assert fallback_tables[0]["quality_flag"] == "raw_text_table"


def test_normalized_statement_tables_include_row_kinds_and_diagnostics(db_session, monkeypatch):
    class FakePage:
        def extract_text(self):
            return "\n".join(
                [
                    "Consolidated Statement of Financial Position",
                    "31 December 2025 31 December 2024",
                ]
            )

        def extract_tables(self):
            return [[
                ["", "31 December 2025", "31 December 2024"],
                ["Current assets", "100", "90"],
                ["Other current assets", "20", "15"],
                ["Total assets", "150", "120"],
            ]]

    class FakePdf:
        pages = [FakePage()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setitem(sys.modules, "pdfplumber", types.SimpleNamespace(open=lambda _path: FakePdf()))
    doc = _document(db_session, "normalized.pdf", b"%PDF fake")

    report = StatementTableExtractor().extract(doc)

    table = report["normalized_statement_tables"][0]
    assert table["table_role"] in {"primary_statement", "primary_statement_degraded_but_usable"}
    assert table["row_blocks"][0]["row_kind"] == "statement_line_item"
    assert "grand_total" in {block["row_kind"] for block in table["row_blocks"]}
    assert "label_column_missing_count" in report["table_structure_diagnostics"]


def test_notes_profit_or_loss_page_does_not_get_income_text_fallback(db_session, monkeypatch):
    class NotesPage:
        def extract_text(self):
            return "\n".join(
                [
                    "Notes to Consolidated Financial Statements",
                    "Impairment loss is included in statement of profit or loss and other comprehensive income.",
                    "Sales and other operating revenues 100 90",
                ]
            )

        def extract_tables(self):
            return [[["Current income taxes"], ["Deferred income taxes"]]]

    class FakePdf:
        pages = [NotesPage()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setitem(sys.modules, "pdfplumber", types.SimpleNamespace(open=lambda _path: FakePdf()))
    doc = _document(db_session, "notes-income.pdf", b"%PDF fake")

    report = StatementTableExtractor().extract(doc)

    assert all(table["extraction_method"] != "text_table_fallback" for table in report["statement_tables"])


def test_gazp_like_primary_balance_sheet_page_gets_text_fallback_with_unit_metadata(db_session, monkeypatch):
    class BalanceSheetPage:
        def extract_text(self):
            return "\n".join(
                [
                    "PJSC Gazprom",
                    "Consolidated Balance Sheet",
                    "(in millions of Russian Roubles)",
                    "31 December 2021 31 December 2020",
                    "Current assets 8,252,330 6,214,791",
                    "Current liabilities 5,875,908 4,423,165",
                    "Total equity 14,982,643 12,701,644",
                ]
            )

        def extract_tables(self):
            return []

    class FakePdf:
        pages = [BalanceSheetPage()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setitem(sys.modules, "pdfplumber", types.SimpleNamespace(open=lambda _path: FakePdf()))
    doc = _document(db_session, "gazp-balance.pdf", b"%PDF fake")

    report = StatementTableExtractor().extract(doc)

    table = report["statement_tables"][0]
    assert table["statement_type"] == "balance_sheet"
    assert table["extraction_method"] == "text_table_fallback"
    assert table["quality_flag"] == "raw_text_table"
    assert table["eligible_for_fact_normalization"] is False
    assert table["unit"] == "million"
    assert table["currency"] == "RUB"
    assert table["unit_multiplier"] == 1_000_000
    assert report["facts_extracted"] == 0
    assert report["fact_parser_status"] == "not_invoked"


def test_gazp_like_primary_income_and_cash_flow_pages_get_text_fallback(db_session, monkeypatch):
    class IncomePage:
        def extract_text(self):
            return "\n".join(
                [
                    "PJSC Gazprom",
                    "Consolidated Statement of Comprehensive Income",
                    "(in millions of Russian Rubles)",
                    "Year ended 31 December 2021 2020",
                    "Sales 10,241,353 6,321,559",
                    "Profit for the year 2,093,167 162,414",
                ]
            )

        def extract_tables(self):
            return []

    class CashFlowPage:
        def extract_text(self):
            return "\n".join(
                [
                    "PJSC Gazprom",
                    "Consolidated Statement of Cash Flows",
                    "(RUB mln)",
                    "Cash flows from operating activities",
                    "Net cash provided by operating activities 3,015,390 1,935,337",
                    "Net cash used in investing activities",
                    "Net cash used in financing activities",
                    "Cash and cash equivalents at end of period",
                ]
            )

        def extract_tables(self):
            return []

    class FakePdf:
        pages = [IncomePage(), CashFlowPage()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setitem(sys.modules, "pdfplumber", types.SimpleNamespace(open=lambda _path: FakePdf()))
    doc = _document(db_session, "gazp-income-cash.pdf", b"%PDF fake")

    report = StatementTableExtractor().extract(doc)

    by_type = {table["statement_type"]: table for table in report["statement_tables"]}
    assert by_type["income_statement"]["extraction_method"] == "text_table_fallback"
    assert by_type["cash_flow"]["extraction_method"] == "text_table_fallback"
    assert by_type["cash_flow"]["unit_multiplier"] == 1_000_000


def test_sber_like_russian_billion_income_page_gets_text_fallback_when_grid_empty(db_session, monkeypatch):
    class IncomePage:
        def extract_text(self):
            return "\n".join(
                [
                    "Обобщенный консолидированный отчет о прибылях и убытках",
                    "За год, закончившийся 31 декабря",
                    "в миллиардах российских рублей Прим. 2025 года 2024 года",
                    "Прибыль за год 1 705,9 1 580,3",
                ]
            )

        def extract_tables(self):
            return [[["Обобщенный консолидированный отчет о прибылях и убытках"]]]

    class FakePdf:
        pages = [IncomePage()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setitem(sys.modules, "pdfplumber", types.SimpleNamespace(open=lambda _path: FakePdf()))
    doc = _document(db_session, "sber-income.pdf", b"%PDF fake")
    doc.report_period = "2025"

    report = StatementTableExtractor().extract(doc)

    fallback_tables = [table for table in report["statement_tables"] if table["extraction_method"] == "text_table_fallback"]
    assert fallback_tables
    table = fallback_tables[0]
    assert table["statement_type"] == "income_statement"
    assert table["unit"] == "billion"
    assert table["currency"] == "RUB"
    assert table["unit_multiplier"] == 1_000_000_000


def test_toc_page_with_statement_names_does_not_become_primary_statement_fallback(db_session, monkeypatch):
    class TocPage:
        def extract_text(self):
            return "\n".join(
                [
                    "Contents",
                    "Consolidated Balance Sheet ........................................................................ 8",
                    "Consolidated Statement of Comprehensive Income ........................................ 9",
                    "Consolidated Statement of Cash Flows ..................................................... 10",
                    "Notes to the Consolidated Financial Statements:",
                ]
            )

        def extract_tables(self):
            return []

    class FakePdf:
        pages = [TocPage()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setitem(sys.modules, "pdfplumber", types.SimpleNamespace(open=lambda _path: FakePdf()))
    doc = _document(db_session, "toc.pdf", b"%PDF fake")

    report = StatementTableExtractor().extract(doc)

    assert report["tables_extracted"] == 0
    assert report["statement_tables"] == []


def test_image_only_expected_primary_page_produces_diagnostic_not_fake_table(db_session, monkeypatch):
    class TocPage:
        def extract_text(self):
            return "\n".join(
                [
                    "Contents",
                    "Consolidated Balance Sheet ........................................................................ 2",
                ]
            )

        def extract_tables(self):
            return []

        def extract_words(self):
            return [{"text": "Contents"}]

    class ImageOnlyPage:
        def extract_text(self):
            return ""

        def extract_tables(self):
            return []

        def extract_words(self):
            return []

    class FakePdf:
        pages = [TocPage(), ImageOnlyPage()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setitem(sys.modules, "pdfplumber", types.SimpleNamespace(open=lambda _path: FakePdf()))
    doc = _document(db_session, "image-only.pdf", b"%PDF fake")

    report = StatementTableExtractor().extract(doc)

    assert report["statement_tables"] == []
    assert any("primary_statement_page_image_only_or_no_extractable_text" in warning for warning in report["warnings"])


def test_pdf_extraction_normalizes_duplicate_headers_and_reconstructs_label_column(db_session, monkeypatch):
    class FakePage:
        def extract_text(self):
            return "\n".join(
                [
                    "Консолидированный отчет о финансовом положении",
                    "В тысячах российских рублей",
                ]
            )

        def extract_tables(self):
            return [
                [
                    ["", "", "", "Прим.", "", "", "", "31 декабря", "", "", "31 декабря", ""],
                    ["", "", "", "", "", "", "", "2025 г.", "", "", "2024 г.", ""],
                    ["Денежные", "средства и их", "эквиваленты", "5", "", "", "", "451 406 356", "", "", "400 000 000", ""],
                    ["ИТОГО", "АКТИВЫ", "", "", "", "", "", "1 040 442 406", "", "", "900 000 000", ""],
                ]
            ]

    class FakePdf:
        pages = [FakePage()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setitem(sys.modules, "pdfplumber", types.SimpleNamespace(open=lambda _path: FakePdf()))
    doc = _document(db_session, "normalized.pdf", b"%PDF fake")

    report = StatementTableExtractor().extract(doc)

    table = report["statement_tables"][0]
    first_row = table["rows"][0]
    second_row = table["rows"][1]

    assert table["statement_type"] == "balance_sheet"
    assert table["header_row_count"] >= 2
    assert table["label_column_present"] is True
    assert table["label_column_reconstructed"] is True
    assert table["repeated_column_names_detected"] is True
    assert "line" in table["normalized_columns"]
    assert any("2025" in column for column in table["normalized_columns"])
    assert any("2024" in column for column in table["normalized_columns"])
    assert first_row["line"] == "Денежные средства и их эквиваленты"
    assert second_row["line"] == "ИТОГО АКТИВЫ"


def test_pdf_extraction_stitches_split_rows_and_resolves_period(db_session, monkeypatch):
    class FakePage:
        def extract_text(self):
            return "\n".join(
                [
                    "Консолидированный отчет о прибылях и убытках",
                    "за год, закончившийся 31 декабря 2025 г.",
                    "в тысячах российских рублей",
                ]
            )

        def extract_tables(self):
            return [
                [
                    ["", "", "", "Прим.", "", "", "", "2025 г.", "", "", "2024 г.", ""],
                    ["Выручка", "", "", "24", "", "", "", "3 509 225 556", "", "", "3 043 433 503", ""],
                    ["Коммерческие, общехозяйственные и административные", "", "", "", "", "", "", "", "", "(585 938 367)", ""],
                    ["расходы", "", "", "25", "", "", "", "(707 561 357)", "", "", "(585 938 367)", ""],
                ]
            ]

    class FakePdf:
        pages = [FakePage()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setitem(sys.modules, "pdfplumber", types.SimpleNamespace(open=lambda _path: FakePdf()))
    doc = _document(db_session, "stitched-income.pdf", b"%PDF fake")
    doc.report_period = "2026Q4"

    report = StatementTableExtractor().extract(doc)

    table = report["statement_tables"][0]
    stitched = next(row for row in table["rows"] if row.get("stitched_from_rows"))

    assert table["statement_type"] == "income_statement"
    assert table["effective_period"] == "2025Q4"
    assert table["comparative_period"] == "2024Q4"
    assert table["period_source"] in {"table_headers", "document_text"}
    assert stitched["line"] == "Коммерческие, общехозяйственные и административные расходы"
    assert stitched["2025 г."] == "(707 561 357)"
    assert stitched["2024 г."] == "(585 938 367)"


def test_income_rows_with_explicit_years_can_classify_without_strong_title(db_session, monkeypatch):
    class FakePage:
        def extract_text(self):
            return "в тысячах российских рублей"

        def extract_tables(self):
            return [
                [
                    ["", "2025 г.", "2024 г."],
                    ["Выручка", "3 509 225 556", "3 043 433 503"],
                    ["Валовая прибыль", "788 199 519", "686 822 515"],
                    ["Прибыль за год", "35 000 000", "28 000 000"],
                ]
            ]

    class FakePdf:
        pages = [FakePage()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setitem(sys.modules, "pdfplumber", types.SimpleNamespace(open=lambda _path: FakePdf()))
    doc = _document(db_session, "income-rows.pdf", b"%PDF fake")
    doc.report_period = "2025Q4"

    report = StatementTableExtractor().extract(doc)

    assert report["statement_tables"][0]["statement_type"] == "income_statement"
