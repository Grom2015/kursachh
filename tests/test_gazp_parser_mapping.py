from app.db.models import ReportDocument
from app.services.parsing.gazp_ifrs_pdf_parser import GAZPIFRSPDFParser


def doc(period: str = "2021Q2") -> ReportDocument:
    return ReportDocument(
        id=1,
        company_id=1,
        report_period=period,
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="issuer_ir_manifest",
        source_url="https://www.gazprom.com/report.pdf",
        storage_path="report.pdf",
    )


def test_gazp_parser_extracts_revenue_with_raw_label_and_source_location():
    parser = GAZPIFRSPDFParser()
    text = "\n".join(
        [
            "PJSC Gazprom",
            "7 Segment Information (continued)",
            "Total sales in the consolidated interim condensed",
            "statement of comprehensive income 2,066,807 1,163,316 4,351,968 2,903,148",
        ]
    )

    facts = parser._extract_from_statement_text(doc("2021Q2"), text, 14)

    fact = next(item for item in facts if item.metric_code == "revenue")
    assert fact.value == 4_351_968
    assert fact.metric_name_original == "Total sales in the consolidated interim condensed statement of comprehensive income"
    assert fact.source_location["raw_label"] == fact.metric_name_original
    assert fact.source_location["page"] == 14
    assert fact.period_type == "ytd"
    assert fact.confidence_score >= 0.8


def test_gazp_parser_does_not_extract_net_income_from_eps_subtotal():
    parser = GAZPIFRSPDFParser()
    text = "\n".join(
        [
            "PJSC Gazprom",
            "33 Basic and Diluted Earnings per Share",
            "Profit for the year attributable to the owners of PJSC Gazprom 2,093,071 135,341",
        ]
    )

    facts = parser._extract_from_statement_text(doc("2021Q4"), text, 55)

    assert [fact.metric_code for fact in facts] == []


def test_gazp_parser_extracts_balance_sheet_snapshot_facts_from_segment_reconciliation():
    parser = GAZPIFRSPDFParser()
    text = "\n".join(
        [
            "PJSC Gazprom",
            "7 Segment Information (continued)",
            "The reconciliation of reportable segments’ liabilities to total liabilities in the consolidated balance sheet is",
            "provided below.",
            "31 December",
            "Notes 2021 2020",
            "21 Short-term borrowings, promissory notes and current portion of long-term borrowings 697,046 693,534",
            "22 Long-term borrowings, promissory notes 4,186,656 4,214,080",
        ]
    )

    facts = parser._extract_from_statement_text(doc("2021Q4"), text, 32)

    by_code = {fact.metric_code: fact for fact in facts}
    assert by_code["short_term_borrowings"].value == 697_046
    assert by_code["long_term_borrowings"].value == 4_186_656
    assert by_code["short_term_borrowings"].period_type == "balance_sheet_snapshot"
    assert by_code["long_term_borrowings"].source_location["table_title"] == "segment_information_note"


def test_gazp_ambiguous_profit_label_stays_diagnostic_not_canonical():
    parser = GAZPIFRSPDFParser()
    text = "\n".join(
        [
            "PJSC Gazprom",
            "7 Segment Information (continued)",
            "Financial result of reportable segments 2,397,714 108,316",
        ]
    )

    facts = parser._extract_from_statement_text(doc("2021Q4"), text, 30)

    assert facts == []
