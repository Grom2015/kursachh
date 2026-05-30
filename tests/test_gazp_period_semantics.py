from app.db.models import ReportDocument
from app.services.parsing.gazp_ifrs_pdf_parser import GAZPIFRSPDFParser


def doc(period: str) -> ReportDocument:
    return ReportDocument(
        report_period=period,
        reporting_standard="IFRS",
        source_type="issuer_ir_manifest",
        source_role="financial_statements",
        storage_path="report.pdf",
    )


def test_gazp_period_type_is_ytd_for_3m_6m_9m_profit_loss_and_cash_flow():
    parser = GAZPIFRSPDFParser()

    assert parser._period_semantics(doc("2021Q1"), "revenue") == ("ytd", 3)
    assert parser._period_semantics(doc("2021Q2"), "capex") == ("ytd", 6)
    assert parser._period_semantics(doc("2021Q3"), "operating_cash_flow") == ("ytd", 9)


def test_gazp_period_type_is_annual_for_fy_profit_loss_and_cash_flow():
    parser = GAZPIFRSPDFParser()

    assert parser._period_semantics(doc("2021Q4"), "revenue") == ("annual", 12)
    assert parser._period_semantics(doc("2021Q4"), "operating_cash_flow") == ("annual", 12)


def test_gazp_balance_sheet_metrics_are_snapshots():
    parser = GAZPIFRSPDFParser()

    assert parser._period_semantics(doc("2021Q2"), "total_assets") == ("balance_sheet_snapshot", None)
    assert parser._period_semantics(doc("2021Q4"), "total_debt") == ("balance_sheet_snapshot", None)
