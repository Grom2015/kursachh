from app.db.models import Company, ReportDocument
from app.services.parsing.lkoh_ifrs_pdf_parser import LKOHIFRSPDFParser


def _document(source_role="financial_statements", period="2021Q2"):
    doc = ReportDocument(
        id=10,
        company_id=1,
        report_period=period,
        reporting_standard="IFRS",
        document_type="financial_statement" if source_role == "financial_statements" else "press_release",
        source_role=source_role,
        source_type="issuer_ir_manifest",
        source_url="https://www.lukoil.com/report.pdf",
        storage_path="report.pdf",
    )
    doc.company = Company(ticker="LKOH", board="TQBR", short_name="LKOH", full_name="LKOH")
    return doc


def test_strong_table_row_match_creates_high_confidence_fact():
    text = """PJSC LUKOIL
Consolidated Statement of Profit or Loss and Other Comprehensive Income
(Millions of Russian rubles)
For the three For the three For the six For the six
months ended months ended months ended months ended
30 June 2021 30 June 2020 30 June 2021 30 June 2020
Revenues
Sales (including excise and export tariffs) 27 2,201,884 986,427 4,078,367 2,652,412
"""
    facts = LKOHIFRSPDFParser()._extract_from_statement_text(_document(), text, 5)
    fact = next(item for item in facts if item.metric_code == "revenue")
    assert fact.value == 4_078_367
    assert fact.quality_flag == "exact"
    assert fact.confidence_score >= 0.8
    assert fact.period_type == "ytd"
    assert fact.source_location["raw_label"] == "Sales (including excise and export tariffs)"
    assert fact.source_location["table"] == "statement_text"


def test_weak_text_match_stays_low_confidence():
    text = "EBITDA was discussed in the report 2021 2020"
    facts = LKOHIFRSPDFParser()._extract_weak_from_text(_document(source_role="other"), text, 1)
    assert facts
    assert facts[0].quality_flag == "low_confidence_parse"


def test_financial_statement_parser_sets_balance_sheet_snapshot_period_type():
    text = """PJSC LUKOIL
Consolidated Statement of Financial Position
(Millions of Russian rubles)
30 June 2021
Assets
Total assets 6,508,931 5,991,579
"""
    facts = LKOHIFRSPDFParser()._extract_from_statement_text(_document(), text, 4)
    fact = next(item for item in facts if item.metric_code == "total_assets")
    assert fact.period_type == "balance_sheet_snapshot"
    assert fact.value == 6_508_931


def test_current_assets_and_liabilities_strong_match():
    text = """PJSC LUKOIL
Consolidated Statement of Financial Position
(Millions of Russian rubles)
Total current assets 1,838,580 1,276,460
Total current liabilities 1,278,284 885,659
"""
    facts = LKOHIFRSPDFParser()._extract_from_statement_text(_document(), text, 4)
    by_code = {fact.metric_code: fact for fact in facts}
    assert by_code["current_assets"].value == 1_838_580
    assert by_code["current_liabilities"].value == 1_278_284
    assert by_code["current_assets"].confidence_score >= 0.8


def test_operating_profit_strong_match():
    text = """PJSC LUKOIL
Consolidated Statement of Profit or Loss and Other Comprehensive Income
(Millions of Russian rubles)
Profit from operating activities 233,328 43,691 433,994 83,816
"""
    facts = LKOHIFRSPDFParser()._extract_from_statement_text(_document(), text, 5)
    fact = next(item for item in facts if item.metric_code == "operating_profit")
    assert fact.value == 433_994
    assert fact.period_type == "ytd"


def test_total_debt_explicit_strong_match():
    text = """PJSC LUKOIL
Consolidated Statement of Financial Position
(Millions of Russian rubles)
Total debt 700,778 659,711
"""
    facts = LKOHIFRSPDFParser()._extract_from_statement_text(_document(), text, 4)
    fact = next(item for item in facts if item.metric_code == "total_debt")
    assert fact.value == 700_778
    assert fact.quality_flag == "exact"


def test_total_debt_derived_from_components():
    parser = LKOHIFRSPDFParser()
    doc = _document()
    text = """PJSC LUKOIL
Consolidated Statement of Financial Position
(Millions of Russian rubles)
Short-term borrowings and current portion of long-term debt 112,868 82,636
Long-term debt 517,910 577,075
"""
    facts = parser._extract_from_statement_text(doc, text, 4)
    facts.extend(parser._derive_total_debt(doc, facts))
    debt = next(item for item in facts if item.metric_code == "total_debt")
    assert debt.value == 630_778
    assert debt.quality_flag == "derived"
    assert debt.source_location["formula"] == "short_term_borrowings + long_term_borrowings"


def test_ambiguous_debt_label_ignored():
    text = """PJSC LUKOIL
Consolidated Statement of Financial Position
(Millions of Russian rubles)
Borrowings 100 90
"""
    facts = LKOHIFRSPDFParser()._extract_from_statement_text(_document(), text, 4)
    assert [fact.metric_code for fact in facts] == []


def test_press_release_parser_does_not_create_canonical_facts(monkeypatch):
    class FakePage:
        def extract_tables(self):
            return []

        def extract_text(self):
            return "Sales (including excise and export tariffs) 1 2"

    class FakePdf:
        pages = [FakePage()]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    class FakePdfPlumber:
        @staticmethod
        def open(path):
            return FakePdf()

    import app.services.parsing.lkoh_ifrs_pdf_parser as parser_module

    monkeypatch.setitem(__import__("sys").modules, "pdfplumber", FakePdfPlumber)
    doc = _document(source_role="press_release")
    doc.storage_path = "release.pdf"
    facts = parser_module.LKOHIFRSPDFParser().parse(doc)
    assert facts == []
