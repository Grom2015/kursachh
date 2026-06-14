import json

from sqlalchemy import select

from app.db.models import Company, ReportDocument
from app.services.parsing.dataframe_statement_parser import (
    BALANCE_SHEET_INTERPRETER,
    CASH_FLOW_INTERPRETER,
    INCOME_STATEMENT_INTERPRETER,
    DataFrameStatementParser,
    StatementFactCandidate,
    StatementRowInterpretationContext,
    is_current_period_column,
    match_metric,
    parse_number,
)
from app.services.parsing.statement_table_extractor import statement_tables_path


def _doc_with_artifact(db_session, tables, ticker="LKOH", period="2021Q4"):
    company = db_session.scalar(select(Company).where(Company.ticker == ticker))
    if not company:
        company = Company(
            ticker=ticker,
            board="TQBR",
            short_name=ticker,
            full_name=ticker,
            aliases_json=[],
            sector="financials" if ticker == "SBER" else "oil_gas",
            subsector="bank" if ticker == "SBER" else None,
        )
        db_session.add(company)
        db_session.flush()
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period=period,
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
        json.dumps({"document_id": doc.id, "period": period, "statement_tables": tables}),
        encoding="utf-8",
    )
    return doc


def _table(statement_type, rows, extraction_method="pdf_table", source_location=None):
    return {
        "document_id": 1,
        "company_ticker": "LKOH",
        "period": "2021Q4",
        "reporting_standard": "IFRS",
        "statement_type": statement_type,
        "period_type": "annual",
        "table_index": 0,
        "page_number": 1,
        "table_title": statement_type,
        "unit": "million",
        "currency": "RUB",
        "columns": ["line", "2021", "2020"],
        "rows": rows,
        "dataframe_json": {"orientation": "records", "data": rows},
        "source_location": source_location if source_location is not None else {"page": 1, "table_index": 0},
        "confidence_score": 0.85,
        "extraction_method": extraction_method,
        "quality_flag": "raw_table",
        "warnings": [],
    }


def _bank_table(statement_type, lines, page_number):
    return {
        "document_id": 1,
        "company_ticker": "SBER",
        "period": "2025Q4",
        "reporting_standard": "IFRS",
        "statement_type": statement_type,
        "period_type": "annual",
        "table_index": page_number,
        "page_number": page_number,
        "table_title": statement_type,
        "unit": "billion",
        "currency": "RUB",
        "unit_multiplier": 1_000_000_000,
        "columns": ["line"],
        "rows": [{"line": line} for line in lines],
        "dataframe_json": {"orientation": "records", "data": [{"line": line} for line in lines]},
        "source_location": {"page": page_number, "table_index": 0, "extraction_method": "text_table_fallback"},
        "confidence_score": 0.85,
        "extraction_method": "text_table_fallback",
        "quality_flag": "raw_text_table",
        "warnings": ["text_table_fallback_used"],
    }


def _fallback_table(statement_type, title, rows, source_location=None, table_index=0):
    return {
        "company_ticker": "LKOH",
        "period": "2021Q4",
        "reporting_standard": "IFRS",
        "statement_type": statement_type,
        "period_type": "annual",
        "table_index": table_index,
        "page_number": 7,
        "table_title": title,
        "unit": "million",
        "currency": "RUB",
        "columns": ["line"],
        "rows": [{"line": row} for row in rows],
        "dataframe_json": {"orientation": "records", "data": [{"line": row} for row in rows]},
        "source_location": source_location if source_location is not None else {"page": 7, "table_index": table_index},
        "confidence_score": 0.8,
        "extraction_method": "text_table_fallback",
        "quality_flag": "raw_text_table",
        "warnings": ["text_table_fallback_used"],
    }


def test_dataframe_parser_extracts_income_statement_facts(db_session):
    _doc_with_artifact(
        db_session,
        [
            _table(
                "income_statement",
                [
                    {"line": "Revenue", "2021": "100", "2020": "90"},
                    {"line": "Operating profit", "2021": "20", "2020": "10"},
                    {"line": "Profit for the year", "2021": "15", "2020": "8"},
                ],
            )
        ],
    )

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4")

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["revenue"].value == 100_000_000
    assert by_code["operating_profit"].value == 20_000_000
    assert by_code["net_income"].value == 15_000_000


def test_dataframe_parser_extracts_explicit_extended_ifrs_metrics(db_session):
    _doc_with_artifact(
        db_session,
        [
            _table(
                "income_statement",
                [
                    {"line": "Gross profit", "2021": "80", "2020": "70"},
                    {"line": "Finance costs", "2021": "(12)", "2020": "(10)"},
                    {"line": "Finance income", "2021": "4", "2020": "3"},
                    {"line": "Total comprehensive income, net of tax", "2021": "14", "2020": "9"},
                    {"line": "Non-controlling interests", "2021": "1", "2020": "1"},
                    {"line": "Income tax expense", "2021": "(5)", "2020": "(4)"},
                ],
            ),
            _table(
                "balance_sheet",
                [
                    {"line": "Inventories", "2021": "21", "2020": "18"},
                    {"line": "Indemnification asset", "2021": "5", "2020": "4"},
                    {"line": "Interest accrued", "2021": "2", "2020": "1"},
                    {"line": "Trade and other receivables", "2021": "12", "2020": "11"},
                    {"line": "Short-term financial investments", "2021": "9", "2020": "8"},
                    {"line": "Deferred tax assets", "2021": "7", "2020": "6"},
                    {"line": "Deferred tax liabilities", "2021": "5", "2020": "4"},
                    {"line": "Trade accounts payable", "2021": "19", "2020": "16"},
                    {"line": "Short-term borrowings", "2021": "14", "2020": "13"},
                    {"line": "Long-term borrowings", "2021": "44", "2020": "40"},
                    {"line": "Short-term lease liabilities", "2021": "10", "2020": "9"},
                    {"line": "Long-term lease liabilities", "2021": "30", "2020": "29"},
                    {"line": "Short-term contract liabilities", "2021": "6", "2020": "5"},
                    {"line": "Current income tax payable", "2021": "3", "2020": "2"},
                    {"line": "Provisions and other liabilities", "2021": "17", "2020": "16"},
                ],
                source_location={"page": 2, "table_index": 1},
            ),
            _table(
                "cash_flow",
                [
                    {"line": "Interest paid", "2021": "(8)", "2020": "(7)"},
                    {"line": "Interest received", "2021": "2", "2020": "1"},
                    {"line": "Income tax paid", "2021": "(6)", "2020": "(5)"},
                    {"line": "Dividends paid", "2021": "(9)", "2020": "(8)"},
                    {"line": "Profit before tax", "2021": "15", "2020": "12"},
                    {"line": "Finance costs, net", "2021": "11", "2020": "10"},
                    {
                        "line": (
                            "Depreciation, amortisation and impairment of property, plant and "
                            "equipment, right-of-use assets, investment properties, other "
                            "intangible assets and goodwill"
                        ),
                        "2021": "16",
                        "2020": "15",
                    },
                    {"line": "Net impairment losses on financial assets", "2021": "3", "2020": "2"},
                    {"line": "Impairment of prepayments", "2021": "1", "2020": "1"},
                    {"line": "Share-based compensation expense", "2021": "2", "2020": "1"},
                    {"line": "Net foreign exchange loss", "2021": "4", "2020": "3"},
                    {"line": "Other non-cash items", "2021": "(1)", "2020": "(1)"},
                    {"line": "Increase in inventories", "2021": "(7)", "2020": "(6)"},
                    {"line": "Increase in trade payable", "2021": "8", "2020": "7"},
                    {"line": "Increase in other accounts payable and contract liabilities", "2021": "9", "2020": "8"},
                    {"line": "Proceeds from short-term financial investments", "2021": "10", "2020": "-"},
                    {"line": "Cash flows from financing activities Proceeds from loans", "2021": "12", "2020": "11"},
                    {"line": "Repayment of loans", "2021": "(13)", "2020": "(12)"},
                    {
                        "line": "Other payments for investing activities - Net cash flows used in investing activities",
                        "2021": "(14)",
                        "2020": "(13)",
                    },
                    {
                        "line": "Dividends paid to non-controlling interests - Net cash flows used in financing activities",
                        "2021": "(15)",
                        "2020": "(14)",
                    },
                    {"line": "Effect of exchange rate changes on cash and cash equivalents", "2021": "3", "2020": "2"},
                    {
                        "line": "Movements in cash and cash equivalents Cash and cash equivalents at the beginning of the year",
                        "2021": "22",
                        "2020": "18",
                    },
                    {"line": "Cash and cash equivalents at the end of the year", "2021": "30", "2020": "22"},
                    {"line": "Net cash flows from operating activities", "2021": "25", "2020": "20"},
                ],
                source_location={"page": 3, "table_index": 2},
            ),
        ],
        ticker="EXT_IFRS",
        period="2021Q4",
    )

    result = DataFrameStatementParser(db_session).parse("EXT_IFRS", "2021Q1", "2021Q4")

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["gross_profit"].value == 80_000_000
    assert by_code["finance_costs"].value == -12_000_000
    assert by_code["finance_income"].value == 4_000_000
    assert by_code["total_comprehensive_income"].value == 14_000_000
    assert by_code["non_controlling_interests"].value == 1_000_000
    assert by_code["income_tax_expense"].value == -5_000_000
    assert by_code["inventories"].value == 21_000_000
    assert by_code["indemnification_asset"].value == 5_000_000
    assert by_code["interest_accrued"].value == 2_000_000
    assert by_code["trade_and_other_receivables"].value == 12_000_000
    assert by_code["short_term_financial_investments"].value == 9_000_000
    assert by_code["deferred_tax_assets"].value == 7_000_000
    assert by_code["deferred_tax_liabilities"].value == 5_000_000
    assert by_code["trade_accounts_payable"].value == 19_000_000
    assert by_code["borrowings_current"].value == 14_000_000
    assert by_code["borrowings_non_current"].value == 44_000_000
    assert by_code["lease_liabilities_current"].value == 10_000_000
    assert by_code["lease_liabilities_non_current"].value == 30_000_000
    assert by_code["contract_liabilities_current"].value == 6_000_000
    assert by_code["income_tax_payable"].value == 3_000_000
    assert by_code["provisions_and_other_liabilities"].value == 17_000_000
    assert by_code["interest_paid"].value == -8_000_000
    assert by_code["interest_received"].value == 2_000_000
    assert by_code["income_tax_paid"].value == -6_000_000
    assert by_code["dividends_paid"].value == -9_000_000
    assert by_code["profit_before_tax"].value == 15_000_000
    assert by_code["depreciation_amortization_and_impairment"].value == 16_000_000
    assert by_code["impairment_of_financial_assets"].value == 3_000_000
    assert by_code["impairment_of_prepayments"].value == 1_000_000
    assert by_code["share_based_compensation_expense"].value == 2_000_000
    assert by_code["net_foreign_exchange_result"].value == 4_000_000
    assert by_code["other_non_cash_items"].value == -1_000_000
    assert by_code["change_in_inventories"].value == -7_000_000
    assert by_code["change_in_trade_payables"].value == 8_000_000
    assert by_code["change_in_other_payables_and_contract_liabilities"].value == 9_000_000
    assert by_code["proceeds_from_short_term_financial_investments"].value == 10_000_000
    assert by_code["proceeds_from_borrowings"].value == 12_000_000
    assert by_code["repayment_of_borrowings"].value == -13_000_000
    assert by_code["investing_cash_flow"].value == -14_000_000
    assert by_code["financing_cash_flow"].value == -15_000_000
    assert by_code["effect_of_exchange_rate_on_cash"].value == 3_000_000
    assert by_code["cash_and_equivalents_beginning_of_period"].value == 22_000_000
    assert by_code["cash_and_equivalents"].value == 30_000_000
    assert by_code["operating_cash_flow"].value == 25_000_000
    assert by_code["cash_and_equivalents"].raw_label == "Cash and cash equivalents at the end of the year"


def test_dataframe_parser_extracts_canonical_russian_statement_facts(db_session):
    _doc_with_artifact(
        db_session,
        [
            _table(
                "income_statement",
                [
                    {"line": "Выручка", "2021": "100", "2020": "90"},
                    {"line": "Операционная прибыль", "2021": "20", "2020": "10"},
                    {"line": "Прибыль до налогообложения", "2021": "15", "2020": "8"},
                    {"line": "Чистая прибыль", "2021": "12", "2020": "6"},
                ],
            ),
            _table(
                "cash_flow",
                [
                    {"line": "Чистые денежные средства, полученные от операционной деятельности", "2021": "25", "2020": "20"},
                    {"line": "Приобретение основных средств", "2021": "(5)", "2020": "(4)"},
                ],
                source_location={"page": 2, "table_index": 1},
            ),
        ],
        ticker="RU_CANON",
        period="2021Q4",
    )

    result = DataFrameStatementParser(db_session).parse("RU_CANON", "2021Q1", "2021Q4")

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["revenue"].value == 100_000_000
    assert by_code["operating_profit"].value == 20_000_000
    assert by_code["profit_before_tax"].value == 15_000_000
    assert by_code["net_income"].value == 12_000_000
    assert by_code["operating_cash_flow"].value == 25_000_000
    assert by_code["capex"].value == -5_000_000


def test_dataframe_parser_strips_generic_statement_prefixes_before_fact_promotion(db_session):
    _doc_with_artifact(
        db_session,
        [
            _table(
                "balance_sheet",
                [
                    {"line": "Non-current liabilities Long-term borrowings", "2021": "44", "2020": "40"},
                ],
            ),
            _table(
                "cash_flow",
                [
                    {
                        "line": "Movements in cash and cash equivalents Cash and cash equivalents at the beginning of the year",
                        "2021": "22",
                        "2020": "18",
                    },
                ],
                source_location={"page": 2, "table_index": 1},
            ),
        ],
        ticker="PREFIXES",
        period="2021Q4",
    )

    result = DataFrameStatementParser(db_session).parse("PREFIXES", "2021Q1", "2021Q4")

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["borrowings_non_current"].value == 44_000_000
    assert by_code["cash_and_equivalents_beginning_of_period"].value == 22_000_000


def test_dataframe_parser_skips_statement_titles_signatures_and_footers_in_text_fallback(db_session):
    _doc_with_artifact(
        db_session,
        [
            _fallback_table(
                "income_statement",
                "Consolidated Statement of Profit or Loss",
                [
                    "Consolidated Statement of Profit or Loss for the year ended 31 December 2023",
                    "Revenue 100 90",
                    "Igor Shekhterman Chief Executive Officer 21 March 2024",
                    "The accompanying notes are the integral part of these consolidated financial statements. 12",
                ],
            )
        ],
        ticker="TXTFLT",
        period="2021Q4",
    )

    result = DataFrameStatementParser(db_session, allow_text_fallback_semantic_gate=True).parse(
        "TXTFLT",
        "2021Q1",
        "2021Q4",
    )

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["revenue"].value == 100_000_000
    skipped_labels = {
        "Consolidated Statement of Profit or Loss for the year ended 31 December 2023",
        "Igor Shekhterman Chief Executive Officer 21 March 2024",
        "The accompanying notes are the integral part of these consolidated financial statements.",
    }
    evidence_labels = {item.get("raw_label") for item in result.unmapped_numeric_evidence}
    rejected_labels = {item.get("raw_label") for item in result.rejected_rows}
    assert evidence_labels.isdisjoint(skipped_labels)
    assert rejected_labels.isdisjoint(skipped_labels)


def test_dataframe_parser_accepts_explicit_industrial_impairment_line_items(db_session):
    _doc_with_artifact(
        db_session,
        [
            _table(
                "income_statement",
                [
                    {"line": "Net impairment losses on financial assets", "2021": "(2)", "2020": "(1)"},
                ],
            )
        ],
        ticker="IMP_IFRS",
        period="2021Q4",
    )

    result = DataFrameStatementParser(db_session).parse("IMP_IFRS", "2021Q1", "2021Q4")

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["impairment_of_financial_assets"].value == -2_000_000


def test_dataframe_parser_uses_safe_positional_fallback_for_recovered_balance_sheet_rows(db_session):
    recovered_rows = [
        {
            "60,298": "615,848",
            "59,573": "538,494",
            "line": "Current assets",
            "label_recovered_from_word_layout": True,
            "ownership_confidence": 0.92,
            "label_confidence": 0.9,
            "value_confidence": 0.86,
        },
        {
            "60,298": "1,768,424",
            "59,573": "1,516,963",
            "line": "Total assets",
            "label_recovered_from_word_layout": True,
            "ownership_confidence": 0.92,
            "label_confidence": 0.9,
            "value_confidence": 0.86,
        },
    ]
    row_blocks = [
        {
            "label_text": row["line"],
            "value_cells": {"60,298": row["60,298"], "59,573": row["59,573"]},
            "row_kind": "grand_total" if "Total" in row["line"] else "statement_line_item",
            "ownership_confidence": 0.92,
            "label_confidence": 0.9,
            "value_confidence": 0.86,
            "source_engine": "pdf_table",
            "source_page": 1,
            "source_table_id": "t-1",
            "diagnostics": {"source_line": f"{row['line']} | {row['60,298']} | {row['59,573']}"},
        }
        for row in recovered_rows
    ]
    table = {
        **_table("balance_sheet", recovered_rows),
        "company_ticker": "LKOH_POS",
        "columns": ["line", "60,298", "59,573"],
        "effective_period": "2025Q4",
        "comparative_period": "2024Q4",
        "row_blocks": row_blocks,
    }
    doc = _doc_with_artifact(db_session, [table], ticker="LKOH_POS", period="2025Q4")

    result = DataFrameStatementParser(db_session).parse("LKOH_POS", "2025Q1", "2025Q4", document_ids=[doc.id])

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["current_assets"].value == 615_848_000_000
    assert by_code["total_assets"].value == 1_768_424_000_000


def test_dataframe_parser_uses_safe_positional_fallback_for_recovered_income_statement_rows(db_session):
    recovered_rows = [
        {
            "807,186": "47,021",
            "703,741": "35,478",
            "line": "Profit before tax",
            "label_recovered_from_word_layout": True,
            "recovery_mode": "word_layout_numeric_signature_match",
            "ownership_confidence": 0.92,
            "label_confidence": 0.9,
            "value_confidence": 0.86,
        },
        {
            "807,186": "38,637",
            "703,741": "51,300",
            "line": "Profit for the year",
            "label_recovered_from_word_layout": True,
            "recovery_mode": "word_layout_numeric_signature_match",
            "ownership_confidence": 0.92,
            "label_confidence": 0.9,
            "value_confidence": 0.86,
        },
    ]
    row_blocks = [
        {
            "label_text": row["line"],
            "value_cells": {"807,186": row["807,186"], "703,741": row["703,741"]},
            "row_kind": "statement_line_item",
            "ownership_confidence": 0.92,
            "label_confidence": 0.9,
            "value_confidence": 0.86,
            "fact_period_confidence": 0.92,
            "source_engine": "pdf_table",
            "source_page": 1,
            "source_table_id": "t-1",
            "diagnostics": {"source_line": f"{row['line']} | {row['807,186']} | {row['703,741']}"},
        }
        for row in recovered_rows
    ]
    table = {
        **_table("income_statement", recovered_rows),
        "columns": ["line", "807,186", "703,741"],
        "effective_period": "2025Q4",
        "comparative_period": "2024Q4",
        "period_confidence": 0.95,
        "row_blocks": row_blocks,
    }
    doc = _doc_with_artifact(db_session, [table], ticker="LKOH_INC_POS", period="2025Q4")

    result = DataFrameStatementParser(db_session).parse("LKOH_INC_POS", "2025Q1", "2025Q4", document_ids=[doc.id])

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["profit_before_tax"].value == 47_021_000_000
    assert by_code["net_income"].value == 38_637_000_000


def test_dataframe_parser_recovers_balance_sheet_label_from_page_layout_numeric_fragment(db_session):
    rows = [
        {"2021": "500", "2020": "400"},
    ]
    row_blocks = [
        {
            "label_text": None,
            "value_cells": {"2021": "500", "2020": "400"},
            "row_kind": "numeric_fragment",
            "ownership_confidence": 0.72,
            "label_confidence": 0.0,
            "value_confidence": 0.86,
            "fact_period_confidence": 0.92,
            "source_engine": "pdf_table",
            "source_page": 1,
            "source_table_id": "t-1",
            "diagnostics": {
                "source_line": "500 | 400",
                "recovery_mode": "word_layout_numeric_signature_match",
                "anchor_hints": ["numeric_signature:500.000000|400.000000"],
                "label_recovery_reason": "label_ownership_unresolved_after_recovery",
            },
        }
    ]
    table = {
        **_table("balance_sheet", rows),
        "effective_period": "2021Q4",
        "comparative_period": "2020Q4",
        "period_confidence": 0.95,
        "row_blocks": row_blocks,
        "page_layout": {
            "page_number": 1,
            "lines": [
                {
                    "text": "Total assets 500 400",
                    "bbox": [24, 140, 320, 160],
                    "confidence": 0.84,
                    "source_engine": "pdf_table",
                    "row_index": 0,
                }
            ],
        },
    }
    doc = _doc_with_artifact(db_session, [table], ticker="LKOH_BS_LAYOUT", period="2021Q4")

    result = DataFrameStatementParser(db_session).parse("LKOH_BS_LAYOUT", "2021Q1", "2021Q4", document_ids=[doc.id])

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["total_assets"].value == 500_000_000
    assert by_code["total_assets"].source_location["source_line"] == "Total assets 500 400"


def test_dataframe_parser_promotes_recovered_numeric_fragment_when_balance_sheet_total_is_explicit(db_session):
    rows = [
        {"2021": "320", "2020": "280"},
    ]
    row_blocks = [
        {
            "label_text": "Total current liabilities",
            "value_cells": {"2021": "320", "2020": "280"},
            "row_kind": "numeric_fragment",
            "ownership_confidence": 0.84,
            "label_confidence": 0.86,
            "value_confidence": 0.86,
            "fact_period_confidence": 0.92,
            "source_engine": "pdf_table",
            "source_page": 1,
            "source_table_id": "t-1",
            "diagnostics": {
                "source_line": "Total current liabilities 320 280",
                "recovery_mode": "word_layout_numeric_signature_match",
                "anchor_hints": ["numeric_signature:320.000000|280.000000"],
            },
        }
    ]
    table = {
        **_table("balance_sheet", rows),
        "effective_period": "2021Q4",
        "comparative_period": "2020Q4",
        "period_confidence": 0.95,
        "row_blocks": row_blocks,
    }
    doc = _doc_with_artifact(db_session, [table], ticker="LKOH_BS_NUMERIC_PROMOTE", period="2021Q4")

    result = DataFrameStatementParser(db_session).parse("LKOH_BS_NUMERIC_PROMOTE", "2021Q1", "2021Q4", document_ids=[doc.id])

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["current_liabilities"].value == 320_000_000
    assert not result.unmapped_numeric_evidence


def test_dataframe_parser_extracts_non_current_assets_from_recovered_balance_sheet_total(db_session):
    rows = [
        {"2021": "1,152", "2020": "978", "line": "Итого внеоборотные активы"},
    ]
    row_blocks = [
        {
            "label_text": "Итого внеоборотные активы",
            "value_cells": {"2021": "1,152", "2020": "978"},
            "row_kind": "grand_total",
            "ownership_confidence": 0.92,
            "label_confidence": 0.92,
            "value_confidence": 0.86,
            "fact_period_confidence": 0.92,
            "source_engine": "pdf_table",
            "source_page": 1,
            "source_table_id": "t-1",
            "diagnostics": {"source_line": "Итого внеоборотные активы | 1,152 | 978"},
        }
    ]
    table = {
        **_table("balance_sheet", rows),
        "effective_period": "2021Q4",
        "comparative_period": "2020Q4",
        "period_confidence": 0.95,
        "row_blocks": row_blocks,
    }
    doc = _doc_with_artifact(db_session, [table], ticker="LKOH_BS_NON_CURRENT", period="2021Q4")

    result = DataFrameStatementParser(db_session).parse("LKOH_BS_NON_CURRENT", "2021Q1", "2021Q4", document_ids=[doc.id])

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["non_current_assets"].value == 1_152_000_000


def test_dataframe_parser_keeps_balance_sheet_numeric_fragment_as_evidence_when_multiple_labels_match(db_session):
    rows = [
        {"2021": "500", "2020": "400"},
    ]
    row_blocks = [
        {
            "label_text": None,
            "value_cells": {"2021": "500", "2020": "400"},
            "row_kind": "numeric_fragment",
            "ownership_confidence": 0.72,
            "label_confidence": 0.0,
            "value_confidence": 0.86,
            "fact_period_confidence": 0.92,
            "source_engine": "pdf_table",
            "source_page": 1,
            "source_table_id": "t-1",
            "diagnostics": {
                "source_line": "500 | 400",
                "recovery_mode": "word_layout_numeric_signature_match",
                "anchor_hints": ["numeric_signature:500.000000|400.000000"],
                "label_recovery_reason": "label_ownership_unresolved_after_recovery",
            },
        }
    ]
    table = {
        **_table("balance_sheet", rows),
        "effective_period": "2021Q4",
        "comparative_period": "2020Q4",
        "period_confidence": 0.95,
        "row_blocks": row_blocks,
        "page_layout": {
            "page_number": 1,
            "lines": [
                {"text": "Total assets 500 400", "bbox": [24, 140, 320, 160], "confidence": 0.84, "source_engine": "pdf_table"},
                {"text": "Total equity 500 400", "bbox": [24, 170, 320, 190], "confidence": 0.84, "source_engine": "pdf_table"},
            ],
        },
    }
    doc = _doc_with_artifact(db_session, [table], ticker="LKOH_BS_LAYOUT_AMBIG", period="2021Q4")

    result = DataFrameStatementParser(db_session).parse("LKOH_BS_LAYOUT_AMBIG", "2021Q1", "2021Q4", document_ids=[doc.id])

    assert not result.facts
    assert result.unmapped_numeric_evidence
    assert result.unmapped_numeric_evidence[0]["raw_label"] is None


def test_dataframe_parser_recovers_income_statement_label_from_page_layout_numeric_fragment(db_session):
    rows = [
        {"2021": "120", "2020": "110"},
    ]
    row_blocks = [
        {
            "label_text": None,
            "value_cells": {"2021": "120", "2020": "110"},
            "row_kind": "numeric_fragment",
            "ownership_confidence": 0.72,
            "label_confidence": 0.0,
            "value_confidence": 0.86,
            "fact_period_confidence": 0.92,
            "source_engine": "pdf_table",
            "source_page": 1,
            "source_table_id": "t-1",
            "diagnostics": {
                "source_line": "120 | 110",
                "recovery_mode": "word_layout_numeric_signature_match",
                "anchor_hints": ["numeric_signature:120.000000|110.000000"],
                "label_recovery_reason": "label_ownership_unresolved_after_recovery",
            },
        }
    ]
    table = {
        **_table("income_statement", rows),
        "effective_period": "2021Q4",
        "comparative_period": "2020Q4",
        "period_confidence": 0.95,
        "row_blocks": row_blocks,
        "page_layout": {
            "page_number": 1,
            "lines": [
                {
                    "text": "Revenue 120 110",
                    "bbox": [24, 140, 320, 160],
                    "confidence": 0.84,
                    "source_engine": "pdf_table",
                    "row_index": 0,
                }
            ],
        },
    }
    doc = _doc_with_artifact(db_session, [table], ticker="LKOH_INC_LAYOUT", period="2021Q4")

    result = DataFrameStatementParser(db_session).parse("LKOH_INC_LAYOUT", "2021Q1", "2021Q4", document_ids=[doc.id])

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["revenue"].value == 120_000_000
    assert by_code["revenue"].source_location["source_line"] == "Revenue 120 110"
    assert "lower_trust_recovered_statement_row" in by_code["revenue"].warnings


def test_dataframe_parser_recovers_cash_flow_label_from_page_layout_numeric_fragment(db_session):
    rows = [
        {"2021": "72", "2020": "64"},
    ]
    row_blocks = [
        {
            "label_text": None,
            "value_cells": {"2021": "72", "2020": "64"},
            "row_kind": "numeric_fragment",
            "ownership_confidence": 0.72,
            "label_confidence": 0.0,
            "value_confidence": 0.86,
            "fact_period_confidence": 0.92,
            "source_engine": "pdf_table",
            "source_page": 1,
            "source_table_id": "t-1",
            "diagnostics": {
                "source_line": "72 | 64",
                "recovery_mode": "word_layout_numeric_signature_match",
                "anchor_hints": ["numeric_signature:72.000000|64.000000"],
                "label_recovery_reason": "label_ownership_unresolved_after_recovery",
            },
        }
    ]
    table = {
        **_table("cash_flow", rows),
        "effective_period": "2021Q4",
        "comparative_period": "2020Q4",
        "period_confidence": 0.95,
        "row_blocks": row_blocks,
        "page_layout": {
            "page_number": 1,
            "lines": [
                {
                    "text": "Net cash generated from operating activities 72 64",
                    "bbox": [24, 140, 420, 160],
                    "confidence": 0.84,
                    "source_engine": "pdf_table",
                    "row_index": 0,
                }
            ],
        },
    }
    doc = _doc_with_artifact(db_session, [table], ticker="LKOH_CF_LAYOUT", period="2021Q4")

    result = DataFrameStatementParser(db_session).parse("LKOH_CF_LAYOUT", "2021Q1", "2021Q4", document_ids=[doc.id])

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["operating_cash_flow"].value == 72_000_000
    assert by_code["operating_cash_flow"].source_location["source_line"] == "Net cash generated from operating activities 72 64"
    assert "lower_trust_recovered_statement_row" in by_code["operating_cash_flow"].warnings


def test_dataframe_parser_matches_degraded_recovered_income_labels_with_inline_numbers(db_session):
    recovered_rows = [
        {
            "807,186": "47,021",
            "703,741": "35,478",
            "line": "Прибыль до налогообложения 47,021 35,478",
            "label_recovered_from_word_layout": True,
            "recovery_mode": "word_layout_numeric_signature_match",
            "ownership_confidence": 0.92,
            "label_confidence": 0.9,
            "value_confidence": 0.86,
        },
    ]
    row_blocks = [
        {
            "label_text": recovered_rows[0]["line"],
            "value_cells": {"807,186": "47,021", "703,741": "35,478"},
            "row_kind": "statement_line_item",
            "ownership_confidence": 0.92,
            "label_confidence": 0.9,
            "value_confidence": 0.86,
            "fact_period_confidence": 0.92,
            "source_engine": "pdf_table",
            "source_page": 1,
            "source_table_id": "t-1",
            "diagnostics": {
                "source_line": "Прибыль до налогообложения 47,021 35,478",
            },
        }
    ]
    table = {
        **_table("income_statement", recovered_rows),
        "columns": ["line", "807,186", "703,741"],
        "effective_period": "2025Q4",
        "comparative_period": "2024Q4",
        "period_confidence": 0.95,
        "row_blocks": row_blocks,
    }
    doc = _doc_with_artifact(db_session, [table], ticker="LKOH_INC_DEG", period="2025Q4")

    result = DataFrameStatementParser(db_session).parse("LKOH_INC_DEG", "2025Q1", "2025Q4", document_ids=[doc.id])

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["profit_before_tax"].value == 47_021_000_000


def test_dataframe_parser_splits_single_merged_income_statement_line_into_multiple_facts(db_session):
    rows = [
        {"line": "Revenue 120 110 Operating profit 20 10"},
    ]
    row_blocks = [
        {
            "label_text": "Revenue 120 110 Operating profit 20 10",
            "value_cells": {},
            "row_kind": "statement_line_item",
            "row_confidence": 0.78,
            "label_confidence": 0.78,
            "ownership_confidence": 0.8,
            "fact_period_confidence": 0.92,
            "source_engine": "ocr_text_engine",
            "source_page": 1,
            "source_table_id": "ocr:1:0",
            "fusion_status": "single_engine",
            "source_engines_involved": ["ocr_text_engine"],
            "diagnostics": {"source_line": "Revenue 120 110 Operating profit 20 10"},
        }
    ]
    table = {
        **_table("income_statement", rows),
        "effective_period": "2021Q4",
        "comparative_period": "2020Q4",
        "period_confidence": 0.95,
        "row_blocks": row_blocks,
        "page_layout": {
            "page_number": 1,
            "lines": [
                {
                    "text": "Revenue 120 110 Operating profit 20 10",
                    "bbox": [24, 140, 420, 160],
                    "confidence": 0.84,
                    "source_engine": "ocr_text_engine",
                    "row_index": 0,
                }
            ],
            "layout_diagnostics": {"merged_line_detected": True},
        },
    }
    doc = _doc_with_artifact(db_session, [table], ticker="LKOH_INC_MERGED", period="2021Q4")

    result = DataFrameStatementParser(db_session).parse("LKOH_INC_MERGED", "2021Q1", "2021Q4", document_ids=[doc.id])

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["revenue"].value == 120_000_000
    assert by_code["operating_profit"].value == 20_000_000
    assert by_code["revenue"].source_location["source_line"] == "Revenue 120 110 Operating profit 20 10"
    assert "lower_trust_recovered_statement_row" in by_code["operating_profit"].warnings


def test_dataframe_parser_splits_single_merged_cash_flow_line_into_multiple_facts(db_session):
    merged_line = "Net cash generated from operating activities 72 64 Purchases of property plant and equipment (10) (8)"
    rows = [
        {"line": merged_line},
    ]
    row_blocks = [
        {
            "label_text": merged_line,
            "value_cells": {},
            "row_kind": "statement_line_item",
            "row_confidence": 0.78,
            "label_confidence": 0.78,
            "ownership_confidence": 0.8,
            "fact_period_confidence": 0.92,
            "source_engine": "ocr_text_engine",
            "source_page": 1,
            "source_table_id": "ocr:1:0",
            "fusion_status": "single_engine",
            "source_engines_involved": ["ocr_text_engine"],
            "diagnostics": {"source_line": merged_line},
        }
    ]
    table = {
        **_table("cash_flow", rows),
        "effective_period": "2021Q4",
        "comparative_period": "2020Q4",
        "period_confidence": 0.95,
        "row_blocks": row_blocks,
        "page_layout": {
            "page_number": 1,
            "lines": [
                {
                    "text": merged_line,
                    "bbox": [24, 140, 560, 160],
                    "confidence": 0.84,
                    "source_engine": "ocr_text_engine",
                    "row_index": 0,
                }
            ],
            "layout_diagnostics": {"merged_line_detected": True},
        },
    }
    doc = _doc_with_artifact(db_session, [table], ticker="LKOH_CF_MERGED", period="2021Q4")

    result = DataFrameStatementParser(db_session).parse("LKOH_CF_MERGED", "2021Q1", "2021Q4", document_ids=[doc.id])

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["operating_cash_flow"].value == 72_000_000
    assert by_code["capex"].value == -10_000_000


def test_dataframe_parser_uses_geometry_tokens_to_split_merged_income_statement_line(db_session):
    rows = [
        {"line": "Revenue 120 110 Operating profit 20 10"},
    ]
    row_blocks = [
        {
            "label_text": "Revenue 120 110 Operating profit 20 10",
            "value_cells": {},
            "row_kind": "statement_line_item",
            "row_confidence": 0.78,
            "label_confidence": 0.78,
            "ownership_confidence": 0.8,
            "fact_period_confidence": 0.92,
            "source_engine": "ocr_text_engine",
            "source_page": 1,
            "source_table_id": "ocr:1:0",
            "fusion_status": "single_engine",
            "source_engines_involved": ["ocr_text_engine"],
            "diagnostics": {"source_line": "Revenue 120 110 Operating profit 20 10"},
        }
    ]
    table = {
        **_table("income_statement", rows),
        "effective_period": "2021Q4",
        "comparative_period": "2020Q4",
        "period_confidence": 0.95,
        "row_blocks": row_blocks,
        "page_layout": {
            "page_number": 1,
            "tokens": [
                {"text": "Revenue", "bbox": [24, 140, 90, 160], "source_engine": "ocr_text_engine", "row_index": 0},
                {"text": "120", "bbox": [240, 140, 280, 160], "source_engine": "ocr_text_engine", "row_index": 0},
                {"text": "110", "bbox": [320, 140, 360, 160], "source_engine": "ocr_text_engine", "row_index": 0},
                {"text": "Operating", "bbox": [28, 168, 120, 188], "source_engine": "ocr_text_engine", "row_index": 0},
                {"text": "profit", "bbox": [128, 168, 180, 188], "source_engine": "ocr_text_engine", "row_index": 0},
                {"text": "20", "bbox": [240, 168, 270, 188], "source_engine": "ocr_text_engine", "row_index": 0},
                {"text": "10", "bbox": [320, 168, 350, 188], "source_engine": "ocr_text_engine", "row_index": 0},
            ],
            "lines": [
                {
                    "text": "Revenue 120 110 Operating profit 20 10",
                    "bbox": [24, 140, 420, 188],
                    "confidence": 0.84,
                    "source_engine": "ocr_text_engine",
                    "row_index": 0,
                }
            ],
            "layout_diagnostics": {"merged_line_detected": True, "token_geometry_available": True},
        },
    }
    doc = _doc_with_artifact(db_session, [table], ticker="LKOH_INC_GEOM_SPLIT", period="2021Q4")

    result = DataFrameStatementParser(db_session).parse(
        "LKOH_INC_GEOM_SPLIT",
        "2021Q1",
        "2021Q4",
        document_ids=[doc.id],
    )

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["revenue"].value == 120_000_000
    assert by_code["operating_profit"].value == 20_000_000


def test_dataframe_parser_prefers_normalized_statement_tables_without_legacy_rows(db_session):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2021Q4",
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
                "period": "2021Q4",
                "normalized_statement_tables": [
                    {
                        "document_id": doc.id,
                        "company_ticker": "LKOH",
                        "period": "2021Q4",
                        "effective_period": "2021Q4",
                        "comparative_period": "2020Q4",
                        "reporting_standard": "IFRS",
                        "statement_type": "income_statement",
                        "period_type": "annual",
                        "table_index": 0,
                        "page_number": 1,
                        "table_title": "income_statement",
                        "unit": "million",
                        "currency": "RUB",
                        "columns": ["line", "2021", "2020"],
                        "rows": [],
                        "row_blocks": [
                            {
                                "label_text": "Revenue",
                                "value_cells": {"2021": "100", "2020": "90"},
                                "row_kind": "statement_line_item",
                                "row_confidence": 0.85,
                                "source_engine": "native_pdf_table_engine",
                                "source_page": 1,
                                "source_table_id": "1:1:0",
                                "source_bbox": {"x0": 10, "x1": 100, "top": 20, "bottom": 30},
                                "fusion_status": "single_engine",
                                "source_engines_involved": ["native_pdf_table_engine"],
                                "diagnostics": {"source_line": "Revenue | 100 | 90"},
                            }
                        ],
                        "dataframe_json": {"orientation": "records", "data": []},
                        "source_traceability": {
                            "source_engine": "native_pdf_table_engine",
                            "source_page": 1,
                            "source_table_id": "1:1:0",
                            "source_bbox": None,
                            "fusion_status": "single_engine",
                            "source_engines_involved": ["native_pdf_table_engine"],
                        },
                        "source_location": {"page": 1, "table_index": 0},
                        "confidence_score": 0.85,
                        "extraction_method": "pdf_table",
                        "quality_flag": "degraded_pdf_table",
                        "warnings": [],
                    }
                ],
                "statement_tables": [],
            }
        ),
        encoding="utf-8",
    )

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4")

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["revenue"].value == 100_000_000
    assert by_code["revenue"].source_location["source_engine"] == "native_pdf_table_engine"
    assert by_code["revenue"].source_location["source_table_id"] == "1:1:0"


def test_dataframe_parser_prefers_row_blocks_over_legacy_rows_when_both_exist(db_session):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2021Q4",
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
                "period": "2021Q4",
                "normalized_statement_tables": [
                    {
                        "document_id": doc.id,
                        "company_ticker": "LKOH",
                        "period": "2021Q4",
                        "effective_period": "2021Q4",
                        "comparative_period": "2020Q4",
                        "reporting_standard": "IFRS",
                        "statement_type": "income_statement",
                        "statement_family": "income_statement",
                        "period_type": "annual",
                        "table_index": 0,
                        "page_number": 1,
                        "table_title": "income_statement",
                        "unit": "million",
                        "currency": "RUB",
                        "unit_multiplier": 1_000_000,
                        "columns": ["line", "2021", "2020"],
                        "rows": [{"line": "Revenue", "2021": "1", "2020": "1"}],
                        "row_blocks": [
                            {
                                "label_text": "Revenue",
                                "value_cells": {"2021": "100", "2020": "90"},
                                "row_kind": "statement_line_item",
                                "row_confidence": 0.85,
                                "source_engine": "native_pdf_table_engine",
                                "source_page": 1,
                                "source_table_id": "1:1:0",
                                "source_bbox": None,
                                "fusion_status": "single_engine",
                                "source_engines_involved": ["native_pdf_table_engine"],
                                "diagnostics": {"source_line": "Revenue | 100 | 90"},
                            }
                        ],
                        "dataframe_json": {"orientation": "records", "data": [{"line": "Revenue", "2021": "1", "2020": "1"}]},
                        "source_traceability": {
                            "source_engine": "native_pdf_table_engine",
                            "source_page": 1,
                            "source_table_id": "1:1:0",
                            "source_bbox": None,
                            "fusion_status": "single_engine",
                            "source_engines_involved": ["native_pdf_table_engine"],
                        },
                        "source_location": {"page": 1, "table_index": 0},
                        "confidence_score": 0.85,
                        "extraction_method": "pdf_table",
                        "quality_flag": "degraded_pdf_table",
                        "warnings": [],
                    }
                ],
                "statement_tables": [],
            }
        ),
        encoding="utf-8",
    )

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4")

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["revenue"].value == 100_000_000


def test_dataframe_parser_recovers_split_income_statement_rows_from_adjacent_blocks(db_session):
    _doc_with_artifact(
        db_session,
        [
            {
                **_table(
                    "income_statement",
                    [
                        {"line": "Sales and other operating"},
                        {"line": "revenues", "2021": "100", "2020": "90"},
                    ],
                ),
                "row_blocks": [
                    {
                        "label_text": "Sales and other operating",
                        "row_kind": "statement_line_item",
                        "row_confidence": 0.72,
                        "source_engine": "native_pdf_table_engine",
                        "source_page": 1,
                        "source_table_id": "1:1:0",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["native_pdf_table_engine"],
                        "diagnostics": {"source_line": "Sales and other operating"},
                    },
                    {
                        "label_text": "revenues",
                        "value_cells": {"2021": "100", "2020": "90"},
                        "row_kind": "statement_line_item",
                        "row_confidence": 0.8,
                        "source_engine": "native_pdf_table_engine",
                        "source_page": 1,
                        "source_table_id": "1:1:0",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["native_pdf_table_engine"],
                        "diagnostics": {"source_line": "revenues | 100 | 90"},
                    },
                ],
            }
        ],
    )

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4")

    revenue = next(fact for fact in result.facts if fact.metric_code == "revenue")
    assert revenue.value == 100_000_000
    assert revenue.raw_label == "Sales and other operating revenues"


def test_dataframe_parser_recovers_split_cash_flow_rows_from_adjacent_blocks(db_session):
    _doc_with_artifact(
        db_session,
        [
            {
                **_table(
                    "cash_flow",
                    [
                        {"line": "Net cash generated from"},
                        {"line": "operating activities", "2021": "25", "2020": "15"},
                    ],
                ),
                "row_blocks": [
                    {
                        "label_text": "Net cash generated from",
                        "row_kind": "statement_line_item",
                        "row_confidence": 0.72,
                        "source_engine": "native_pdf_table_engine",
                        "source_page": 1,
                        "source_table_id": "1:1:0",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["native_pdf_table_engine"],
                        "diagnostics": {"source_line": "Net cash generated from"},
                    },
                    {
                        "label_text": "operating activities",
                        "value_cells": {"2021": "25", "2020": "15"},
                        "row_kind": "statement_line_item",
                        "row_confidence": 0.8,
                        "source_engine": "native_pdf_table_engine",
                        "source_page": 1,
                        "source_table_id": "1:1:0",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["native_pdf_table_engine"],
                        "diagnostics": {"source_line": "operating activities | 25 | 15"},
                    },
                ],
            }
        ],
    )

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4")

    operating_cash_flow = next(fact for fact in result.facts if fact.metric_code == "operating_cash_flow")
    assert operating_cash_flow.value == 25_000_000
    assert operating_cash_flow.raw_label == "Net cash generated from operating activities"


def test_dataframe_parser_recovers_split_profit_before_tax_rows(db_session):
    _doc_with_artifact(
        db_session,
        [
            {
                **_table(
                    "income_statement",
                    [
                        {"line": "Profit before"},
                        {"line": "tax", "2021": "18", "2020": "11"},
                    ],
                ),
                "row_blocks": [
                    {
                        "label_text": "Profit before",
                        "row_kind": "statement_line_item",
                        "row_confidence": 0.72,
                        "source_engine": "native_pdf_table_engine",
                        "source_page": 1,
                        "source_table_id": "1:1:0",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["native_pdf_table_engine"],
                        "diagnostics": {"source_line": "Profit before"},
                    },
                    {
                        "label_text": "tax",
                        "value_cells": {"2021": "18", "2020": "11"},
                        "row_kind": "statement_line_item",
                        "row_confidence": 0.8,
                        "source_engine": "native_pdf_table_engine",
                        "source_page": 1,
                        "source_table_id": "1:1:0",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["native_pdf_table_engine"],
                        "diagnostics": {"source_line": "tax | 18 | 11"},
                    },
                ],
            }
        ],
    )

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4")

    fact = next(fact for fact in result.facts if fact.metric_code == "profit_before_tax")
    assert fact.value == 18_000_000
    assert fact.raw_label == "Profit before tax"


def test_dataframe_parser_recovers_note_reference_value_tail_rows(db_session):
    _doc_with_artifact(
        db_session,
        [
            {
                **_table(
                    "income_statement",
                    [
                        {"line": "Revenue"},
                        {"line": "Note 2", "2021": "100", "2020": "90"},
                        {"line": "Operating profit", "2021": "20", "2020": "10"},
                    ],
                ),
                "row_blocks": [
                    {
                        "label_text": "Revenue",
                        "row_kind": "statement_line_item",
                        "row_confidence": 0.72,
                        "source_engine": "native_pdf_table_engine",
                        "source_page": 1,
                        "source_table_id": "1:1:0",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["native_pdf_table_engine"],
                        "diagnostics": {"source_line": "Revenue"},
                    },
                    {
                        "label_text": "Note 2",
                        "value_cells": {"2021": "100", "2020": "90"},
                        "row_kind": "note_reference_only",
                        "row_confidence": 0.8,
                        "source_engine": "ocr_table_structure_engine",
                        "source_page": 1,
                        "source_table_id": "1:1:0",
                        "source_bbox": None,
                        "fusion_status": "merged_engines",
                        "source_engines_involved": ["native_pdf_table_engine", "ocr_table_structure_engine"],
                        "diagnostics": {"source_line": "Note 2 | 100 | 90", "warnings": ["ocr_only_lower_trust"]},
                    },
                    {
                        "label_text": "Operating profit",
                        "value_cells": {"2021": "20", "2020": "10"},
                        "row_kind": "statement_line_item",
                        "row_confidence": 0.8,
                        "source_engine": "native_pdf_table_engine",
                        "source_page": 1,
                        "source_table_id": "1:1:0",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["native_pdf_table_engine"],
                        "diagnostics": {"source_line": "Operating profit | 20 | 10"},
                    },
                ],
            }
        ],
    )

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4")

    revenue = next(fact for fact in result.facts if fact.metric_code == "revenue")
    operating_profit = next(fact for fact in result.facts if fact.metric_code == "operating_profit")
    assert revenue.value == 100_000_000
    assert revenue.raw_label == "Revenue"
    assert revenue.source_location["fusion_status"] == "merged_engines"
    assert "ocr_only_lower_trust" in revenue.warnings
    assert operating_profit.value == 20_000_000


def test_dataframe_parser_does_not_merge_into_new_standalone_row(db_session):
    _doc_with_artifact(
        db_session,
        [
            {
                **_table(
                    "income_statement",
                    [
                        {"line": "Revenue"},
                        {"line": "Operating profit", "2021": "20", "2020": "10"},
                    ],
                ),
                "row_blocks": [
                    {
                        "label_text": "Revenue",
                        "row_kind": "statement_line_item",
                        "row_confidence": 0.72,
                        "source_engine": "native_pdf_table_engine",
                        "source_page": 1,
                        "source_table_id": "1:1:0",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["native_pdf_table_engine"],
                        "diagnostics": {"source_line": "Revenue"},
                    },
                    {
                        "label_text": "Operating profit",
                        "value_cells": {"2021": "20", "2020": "10"},
                        "row_kind": "statement_line_item",
                        "row_confidence": 0.8,
                        "source_engine": "native_pdf_table_engine",
                        "source_page": 1,
                        "source_table_id": "1:1:0",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["native_pdf_table_engine"],
                        "diagnostics": {"source_line": "Operating profit | 20 | 10"},
                    },
                ],
            }
        ],
    )

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4")

    assert "revenue" not in {fact.metric_code for fact in result.facts}
    operating_profit = next(fact for fact in result.facts if fact.metric_code == "operating_profit")
    assert operating_profit.raw_label == "Operating profit"


def test_dataframe_parser_allows_ocr_only_semantic_safe_income_row_with_warning(db_session):
    _doc_with_artifact(
        db_session,
        [
            {
                **_table(
                    "income_statement",
                    [
                        {"line": "Revenue", "2021": "100", "2020": "90"},
                    ],
                ),
                "effective_period": "2021Q4",
                "comparative_period": "2020Q4",
                "period_confidence": 0.94,
                "row_blocks": [
                    {
                        "label_text": "Revenue",
                        "value_cells": {"2021": "100", "2020": "90"},
                        "row_kind": "statement_line_item",
                        "row_confidence": 0.82,
                        "label_confidence": 0.88,
                        "ownership_confidence": 0.84,
                        "fact_period_confidence": 0.92,
                        "source_engine": "ocr_table_structure_engine",
                        "source_page": 1,
                        "source_table_id": "ocr:1:0",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["ocr_table_structure_engine"],
                        "diagnostics": {"source_line": "Revenue | 100 | 90"},
                    }
                ],
            }
        ],
    )

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4")

    revenue = next(fact for fact in result.facts if fact.metric_code == "revenue")
    assert revenue.value == 100_000_000
    assert "ocr_only_lower_trust" in revenue.warnings


def test_dataframe_parser_rejects_low_ownership_row_even_when_label_matches(db_session):
    _doc_with_artifact(
        db_session,
        [
            {
                **_table(
                    "income_statement",
                    [
                        {"line": "Revenue", "2021": "100", "2020": "90"},
                    ],
                ),
                "effective_period": "2021Q4",
                "comparative_period": "2020Q4",
                "period_confidence": 0.94,
                "row_blocks": [
                    {
                        "label_text": "Revenue",
                        "value_cells": {"2021": "100", "2020": "90"},
                        "row_kind": "statement_line_item",
                        "row_confidence": 0.82,
                        "label_confidence": 0.88,
                        "ownership_confidence": 0.45,
                        "fact_period_confidence": 0.92,
                        "source_engine": "ocr_table_structure_engine",
                        "source_page": 1,
                        "source_table_id": "ocr:1:0",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["ocr_table_structure_engine"],
                        "diagnostics": {"source_line": "Revenue | 100 | 90"},
                    }
                ],
            }
        ],
    )

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4")

    assert "revenue" not in {fact.metric_code for fact in result.facts}
    assert any(item.reason == "label_ownership_unresolved" for item in result.rejected_candidates)


def test_dataframe_parser_extracts_balance_sheet_facts(db_session):
    _doc_with_artifact(
        db_session,
        [
            _table(
                "balance_sheet",
                [
                    {"line": "Total assets", "2021": "500", "2020": "400"},
                    {"line": "Total equity", "2021": "300", "2020": "250"},
                    {"line": "Total current assets", "2021": "200", "2020": "150"},
                    {"line": "Total current liabilities", "2021": "100", "2020": "80"},
                    {"line": "Cash and cash equivalents", "2021": "50", "2020": "40"},
                ],
            )
        ],
    )

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4")

    codes = {fact.metric_code for fact in result.facts}
    assert {"total_assets", "total_equity", "current_assets", "current_liabilities", "cash_and_equivalents"} <= codes
    assert all(fact.period_type == "balance_sheet_snapshot" for fact in result.facts)


def test_match_metric_does_not_map_non_current_assets_to_current_assets():
    assert match_metric("Внеоборотные активы Основные средства", "balance_sheet") != "current_assets"
    assert match_metric("В необоротные активы О сновные средства", "balance_sheet") != "current_assets"
    assert match_metric("Прочие оборотные активы", "balance_sheet") != "current_assets"
    assert match_metric("Non-current assets Property, plant and equipment", "balance_sheet") != "current_assets"


def test_match_metric_supports_safe_degraded_aliases_without_promoting_segment_context():
    assert match_metric("Operating result", "income_statement", degraded=True) == "operating_profit"
    assert match_metric("Cash generated from operating activities", "cash_flow", degraded=True) == "operating_cash_flow"
    assert match_metric("Segment revenue", "income_statement", degraded=True) is None


def test_match_metric_supports_canonical_russian_labels():
    assert match_metric("Выручка", "income_statement") == "revenue"
    assert match_metric("Операционная прибыль", "income_statement") == "operating_profit"
    assert match_metric("Прибыль за год", "income_statement") == "net_income"
    assert match_metric("Прибыль до налогообложения", "income_statement") == "profit_before_tax"
    assert match_metric("Чистые денежные средства, полученные от операционной деятельности", "cash_flow") == "operating_cash_flow"
    assert match_metric("Приобретение основных средств", "cash_flow") == "capex"
    assert match_metric("Краткосрочные обязательства", "balance_sheet") == "current_liabilities"
    assert match_metric("Итого внеоборотные активы", "balance_sheet") == "non_current_assets"
    assert match_metric("Долгосрочные обязательства", "balance_sheet") == "non_current_liabilities"


def test_statement_family_interpreters_apply_family_specific_rules():
    balance_metric, balance_reason = BALANCE_SHEET_INTERPRETER.interpret(
        StatementRowInterpretationContext(raw_label="Total assets", row_kind="grand_total")
    )
    income_metric, income_reason = INCOME_STATEMENT_INTERPRETER.interpret(
        StatementRowInterpretationContext(
            raw_label="Segment revenue",
            row_kind="statement_line_item",
            issuer_class="industrial_ifrs_retail",
            degraded=True,
        )
    )
    cash_metric, cash_reason = CASH_FLOW_INTERPRETER.interpret(
        StatementRowInterpretationContext(
            raw_label="Cash generated from operating activities",
            row_kind="statement_line_item",
            degraded=True,
        )
    )

    assert (balance_metric, balance_reason) == ("total_assets", None)
    assert (income_metric, income_reason) == (None, "component_row_not_total_metric")
    assert (cash_metric, cash_reason) == ("operating_cash_flow", None)


def test_balance_sheet_interpreter_accepts_canonical_russian_totals():
    metric, reason = BALANCE_SHEET_INTERPRETER.interpret(
        StatementRowInterpretationContext(raw_label="Итого оборотные активы", row_kind="subtotal")
    )

    assert metric == "current_assets"
    assert reason is None


def test_dataframe_parser_extracts_cash_flow_from_grid_only(db_session):
    _doc_with_artifact(
        db_session,
        [
            _table(
                "cash_flow",
                [
                    {"line": "Net cash provided by operating activities", "2021": "25"},
                    {"line": "Purchases of property, plant and equipment", "2021": "(10)"},
                ],
            )
        ],
    )

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4")

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["operating_cash_flow"].value == 25_000_000
    assert by_code["capex"].value == -10_000_000


def test_dataframe_parser_uses_safe_positional_fallback_for_recovered_cash_flow_rows(db_session):
    recovered_rows = [
        {
            "807,186": "72,114",
            "703,741": "64,220",
            "line": "Net cash generated from operating activities",
            "label_recovered_from_word_layout": True,
            "recovery_mode": "word_layout_numeric_signature_match",
            "ownership_confidence": 0.92,
            "label_confidence": 0.9,
            "value_confidence": 0.86,
        },
    ]
    row_blocks = [
        {
            "label_text": recovered_rows[0]["line"],
            "value_cells": {"807,186": "72,114", "703,741": "64,220"},
            "row_kind": "statement_line_item",
            "ownership_confidence": 0.92,
            "label_confidence": 0.9,
            "value_confidence": 0.86,
            "fact_period_confidence": 0.92,
            "source_engine": "pdf_table",
            "source_page": 1,
            "source_table_id": "t-1",
            "diagnostics": {"source_line": "Net cash generated from operating activities | 72,114 | 64,220"},
        }
    ]
    table = {
        **_table("cash_flow", recovered_rows),
        "columns": ["line", "807,186", "703,741"],
        "effective_period": "2025Q4",
        "comparative_period": "2024Q4",
        "period_confidence": 0.95,
        "row_blocks": row_blocks,
    }
    doc = _doc_with_artifact(db_session, [table], ticker="LKOH_CF_POS", period="2025Q4")

    result = DataFrameStatementParser(db_session).parse("LKOH_CF_POS", "2025Q1", "2025Q4", document_ids=[doc.id])

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["operating_cash_flow"].value == 72_114_000_000


def test_dataframe_parser_uses_semantic_positional_fallback_for_explicit_income_statement_row(db_session):
    rows = [
        {"807,186": "120,500", "703,741": "109,200", "line": "Revenue"},
        {"807,186": "18,400", "703,741": "15,100", "line": "Profit for the year"},
    ]
    row_blocks = [
        {
            "label_text": row["line"],
            "value_cells": {"807,186": row["807,186"], "703,741": row["703,741"]},
            "row_kind": "statement_line_item",
            "ownership_confidence": 0.91,
            "label_confidence": 0.93,
            "value_confidence": 0.88,
            "fact_period_confidence": 0.92,
            "source_engine": "ocr_table_structure_engine",
            "source_page": 1,
            "source_table_id": "ocr:1:0",
            "fusion_status": "single_engine",
            "source_engines_involved": ["ocr_table_structure_engine"],
            "diagnostics": {"source_line": f"{row['line']} | {row['807,186']} | {row['703,741']}"},
        }
        for row in rows
    ]
    table = {
        **_table("income_statement", rows),
        "columns": ["line", "807,186", "703,741"],
        "effective_period": "2025Q4",
        "comparative_period": "2024Q4",
        "period_confidence": 0.95,
        "row_blocks": row_blocks,
    }
    doc = _doc_with_artifact(db_session, [table], ticker="LKOH_INC_SEMANTIC_POS", period="2025Q4")

    result = DataFrameStatementParser(db_session).parse(
        "LKOH_INC_SEMANTIC_POS",
        "2025Q1",
        "2025Q4",
        document_ids=[doc.id],
    )

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["revenue"].value == 120_500_000_000
    assert by_code["net_income"].value == 18_400_000_000
    assert "ocr_only_lower_trust" in by_code["revenue"].warnings


def test_dataframe_parser_uses_semantic_positional_fallback_for_explicit_cash_flow_row(db_session):
    rows = [
        {"807,186": "72,114", "703,741": "64,220", "line": "Net cash generated from operating activities"},
    ]
    row_blocks = [
        {
            "label_text": rows[0]["line"],
            "value_cells": {"807,186": "72,114", "703,741": "64,220"},
            "row_kind": "statement_line_item",
            "ownership_confidence": 0.91,
            "label_confidence": 0.92,
            "value_confidence": 0.88,
            "fact_period_confidence": 0.92,
            "source_engine": "ocr_table_structure_engine",
            "source_page": 1,
            "source_table_id": "ocr:1:0",
            "fusion_status": "single_engine",
            "source_engines_involved": ["ocr_table_structure_engine"],
            "diagnostics": {"source_line": "Net cash generated from operating activities | 72,114 | 64,220"},
        }
    ]
    table = {
        **_table("cash_flow", rows),
        "columns": ["line", "807,186", "703,741"],
        "effective_period": "2025Q4",
        "comparative_period": "2024Q4",
        "period_confidence": 0.95,
        "row_blocks": row_blocks,
    }
    doc = _doc_with_artifact(db_session, [table], ticker="LKOH_CF_SEMANTIC_POS", period="2025Q4")

    result = DataFrameStatementParser(db_session).parse(
        "LKOH_CF_SEMANTIC_POS",
        "2025Q1",
        "2025Q4",
        document_ids=[doc.id],
    )

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["operating_cash_flow"].value == 72_114_000_000
    assert "ocr_only_lower_trust" in by_code["operating_cash_flow"].warnings


def test_dataframe_parser_excludes_text_fallback_by_default(db_session):
    _doc_with_artifact(
        db_session,
        [
            _table(
                "cash_flow",
                [{"line": "Net cash provided by operating activities", "2021": "25"}],
                extraction_method="text_table_fallback",
            )
        ],
    )

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4")

    assert result.facts == []
    assert result.rejected_candidates[0].reason == "text_table_fallback_not_eligible_for_fact_normalization"


def test_dataframe_parser_promotes_text_fallback_income_statement_rows_through_unified_gate(db_session):
    _doc_with_artifact(
        db_session,
        [
            {
                **_fallback_table(
                    "income_statement",
                    "Consolidated Statement of Profit or Loss",
                    [
                        "Consolidated Statement of Profit or Loss",
                        "for the year ended 31 December 2023",
                        "(expressed in millions of Russian Roubles, unless otherwise stated)",
                        "Note 2023 2022",
                        "Revenue 24 3,145,859 2,605,232",
                        "Operating profit 178,201 138,118",
                        "Profit before tax 103,860 63,665",
                        "Profit for the year 78,593 45,188",
                    ],
                    table_index=1,
                ),
                "period": "2023Q4",
                "effective_period": "2023Q4",
                "comparative_period": "2022Q4",
                "columns": ["line", "2023Q4", "2022Q4"],
            },
            {
                **_fallback_table(
                    "cash_flow",
                    "Consolidated Statement of Cash Flows",
                    [
                        "Consolidated Statement of Cash Flows",
                        "for the year ended 31 December 2023",
                        "(expressed in millions of Russian Roubles, unless otherwise stated)",
                        "2023 2022",
                        "Net cash from operating activities before changes in working capital 341,711 300,768",
                    ],
                    table_index=2,
                ),
                "period": "2023Q4",
                "effective_period": "2023Q4",
                "comparative_period": "2022Q4",
                "columns": ["line", "2023Q4", "2022Q4"],
            },
        ],
        ticker="X5",
        period="2023Q4",
    )

    result = DataFrameStatementParser(db_session, allow_text_fallback_semantic_gate=True).parse("X5", "2023Q1", "2023Q4")

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["revenue"].value == 3_145_859_000_000
    assert by_code["operating_profit"].value == 178_201_000_000
    assert by_code["profit_before_tax"].value == 103_860_000_000
    assert by_code["net_income"].value == 78_593_000_000
    assert by_code["operating_cash_flow_before_working_capital"].value == 341_711_000_000
    assert by_code["revenue"].extraction_method == "text_table_fallback_semantic_gate"
    assert "lower_trust_text_fallback_statement_row" in by_code["revenue"].warnings
    assert result.text_fallback_facts_created >= 5


def test_dataframe_parser_extracts_cash_flow_hierarchy_levels(db_session):
    _doc_with_artifact(
        db_session,
        [
            {
                **_table(
                    "cash_flow",
                    [
                        {
                            "line": "Net cash from operating activities before changes in working capital",
                            "2023Q4": "341,711",
                            "2022Q4": "300,768",
                        },
                        {"line": "Net cash flows from operations", "2023Q4": "366,154", "2022Q4": "306,692"},
                        {"line": "Net cash flows from operating activities", "2023Q4": "269,277", "2022Q4": "220,924"},
                    ],
                ),
                "period": "2023Q4",
                "effective_period": "2023Q4",
                "comparative_period": "2022Q4",
                "columns": ["line", "2023Q4", "2022Q4"],
            }
        ],
        ticker="CF_HIER",
        period="2023Q4",
    )

    result = DataFrameStatementParser(db_session).parse("CF_HIER", "2023Q1", "2023Q4")

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["operating_cash_flow_before_working_capital"].value == 341_711_000_000
    assert by_code["net_cash_from_operations"].value == 366_154_000_000
    assert by_code["operating_cash_flow"].value == 269_277_000_000


def test_dataframe_parser_splits_leading_balance_sheet_section_transition_rows(db_session):
    _doc_with_artifact(
        db_session,
        [
            {
                **_table(
                    "balance_sheet",
                    [
                        {
                            "line": "1,142,469 1,011,630 Current assets Inventories 15",
                            "2023Q4": "236,826",
                            "2022Q4": "208,661",
                        },
                        {
                            "line": "440,602 340,385 Total assets",
                            "2023Q4": "1,583,071",
                            "2022Q4": "1,352,015",
                        },
                        {
                            "line": "734,882 679,863 Current liabilities Trade accounts payable",
                            "2023Q4": "290,232",
                            "2022Q4": "238,641",
                        },
                        {
                            "line": "638,849 539,010 Total liabilities",
                            "2023Q4": "1,373,731",
                            "2022Q4": "1,218,873",
                        },
                    ],
                    extraction_method="text_table_fallback",
                ),
                "period": "2023Q4",
                "effective_period": "2023Q4",
                "comparative_period": "2022Q4",
                "columns": ["line", "2023Q4", "2022Q4"],
                "quality_flag": "raw_text_table",
                "warnings": ["text_table_fallback_used"],
            }
        ],
        ticker="X5_BS_SPLIT",
        period="2023Q4",
    )

    result = DataFrameStatementParser(db_session, allow_text_fallback_semantic_gate=True).parse(
        "X5_BS_SPLIT",
        "2023Q1",
        "2023Q4",
    )

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["non_current_assets"].value == 1_142_469_000_000
    assert by_code["non_current_assets"].raw_label == "Non-current assets"
    assert by_code["inventories"].value == 236_826_000_000
    assert by_code["inventories"].raw_label == "Inventories"
    assert by_code["current_assets"].value == 440_602_000_000
    assert by_code["current_assets"].raw_label == "Current assets"
    assert by_code["total_assets"].value == 1_583_071_000_000
    assert by_code["non_current_liabilities"].value == 734_882_000_000
    assert by_code["non_current_liabilities"].raw_label == "Non-current liabilities"
    assert by_code["trade_accounts_payable"].value == 290_232_000_000
    assert by_code["trade_accounts_payable"].raw_label == "Trade accounts payable"
    assert by_code["current_liabilities"].value == 638_849_000_000
    assert by_code["current_liabilities"].raw_label == "Current liabilities"
    assert by_code["total_liabilities"].value == 1_373_731_000_000


def test_is_current_period_column_accepts_normalized_period_keys():
    assert is_current_period_column("2023Q4", "2023Q4", "income_statement") is True
    assert is_current_period_column("2022Q4", "2023Q4", "income_statement") is False


def test_dataframe_parser_cleans_leading_numeric_fragments_before_balance_sheet_totals(db_session):
    _doc_with_artifact(
        db_session,
        [
            {
                **_table(
                    "balance_sheet",
                    [
                        {"line": "440,602 340,385 Total assets", "2023Q4": "1,583,071", "2022Q4": "1,352,015"},
                        {"line": "209,340 133,142 Total equity", "2023Q4": "209,340", "2022Q4": "133,142"},
                    ],
                    extraction_method="text_table_fallback",
                ),
                "period": "2023Q4",
                "effective_period": "2023Q4",
                "comparative_period": "2022Q4",
                "columns": ["line", "2023Q4", "2022Q4"],
                "quality_flag": "raw_text_table",
                "warnings": ["text_table_fallback_used"],
            }
        ],
        ticker="X5_LABEL",
        period="2023Q4",
    )

    result = DataFrameStatementParser(db_session, allow_text_fallback_semantic_gate=True).parse("X5_LABEL", "2023Q1", "2023Q4")

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["total_assets"].raw_label == "Total assets"
    assert by_code["total_assets"].value == 1_583_071_000_000
    assert by_code["total_equity"].raw_label == "Total equity"
    assert by_code["total_equity"].value == 209_340_000_000


def test_dataframe_parser_blocks_mixed_balance_sheet_labels_with_unresolved_ownership(db_session):
    _doc_with_artifact(
        db_session,
        [
            {
                **_table(
                    "balance_sheet",
                    [
                        {
                            "line": "734,882 679,863 Current liabilities Trade accounts payable",
                            "2023Q4": "290,232",
                            "2022Q4": "238,641",
                        },
                        {
                            "line": "Assets Non-current assets Property, plant and equipment",
                            "2023Q4": "364,396",
                            "2022Q4": "315,612",
                        },
                        {
                            "line": "638,849 539,010 Total liabilities 1,373,731 1,218,873 Total equity and liabilities",
                            "2023Q4": "1,583,071",
                            "2022Q4": "1,352,015",
                        },
                    ],
                    extraction_method="text_table_fallback",
                ),
                "period": "2023Q4",
                "effective_period": "2023Q4",
                "comparative_period": "2022Q4",
                "columns": ["line", "2023Q4", "2022Q4"],
                "quality_flag": "raw_text_table",
                "warnings": ["text_table_fallback_used"],
            }
        ],
        ticker="X5_OWNERSHIP",
        period="2023Q4",
    )

    result = DataFrameStatementParser(db_session, allow_text_fallback_semantic_gate=True).parse(
        "X5_OWNERSHIP", "2023Q1", "2023Q4"
    )

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["trade_accounts_payable"].value == 290_232_000_000
    assert by_code["property_plant_and_equipment"].value == 364_396_000_000
    assert by_code["total_equity_and_liabilities"].value == 1_583_071_000_000
    assert result.rejected_candidates == []


def test_dataframe_parser_prefers_plain_net_income_over_attributable_fragment(db_session):
    _doc_with_artifact(
        db_session,
        [
            {
                **_table(
                    "income_statement",
                    [
                        {"line": "Profit for the year", "2023Q4": "78,593", "2022Q4": "45,188"},
                        {
                            "line": (
                                "Profit for the year attributable to: "
                                "Equity holders of the parent 78,281 45,199 Non-controlling interests"
                            ),
                            "2023Q4": "312",
                            "2022Q4": "(11)",
                        },
                    ],
                    extraction_method="text_table_fallback",
                ),
                "period": "2023Q4",
                "effective_period": "2023Q4",
                "comparative_period": "2022Q4",
                "columns": ["line", "2023Q4", "2022Q4"],
                "quality_flag": "raw_text_table",
                "warnings": ["text_table_fallback_used"],
            }
        ],
        ticker="X5_NET",
        period="2023Q4",
    )

    result = DataFrameStatementParser(db_session, allow_text_fallback_semantic_gate=True).parse("X5_NET", "2023Q1", "2023Q4")

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["net_income"].raw_label == "Profit for the year"
    assert by_code["net_income"].value == 78_593_000_000
    assert by_code["net_income_attributable_to_parent"].value == 78_281_000_000


def test_dataframe_parser_splits_embedded_cash_flow_statement_rows(db_session):
    _doc_with_artifact(
        db_session,
        [
            {
                **_table(
                    "cash_flow",
                    [
                        {
                            "line": (
                                "Cash flows from financing activities Proceeds from loans "
                                "21 183,594 148,974 Repayment of loans"
                            ),
                            "2023Q4": "(192,007)",
                            "2022Q4": "(210,615)",
                        },
                    ],
                    extraction_method="text_table_fallback",
                ),
                "period": "2023Q4",
                "effective_period": "2023Q4",
                "comparative_period": "2022Q4",
                "columns": ["line", "2023Q4", "2022Q4"],
                "quality_flag": "raw_text_table",
                "warnings": ["text_table_fallback_used"],
            }
        ],
        ticker="X5_SPLIT",
        period="2023Q4",
    )

    result = DataFrameStatementParser(db_session, allow_text_fallback_semantic_gate=True).parse(
        "X5_SPLIT", "2023Q1", "2023Q4"
    )

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["proceeds_from_borrowings"].value == 183_594_000_000
    assert by_code["repayment_of_borrowings"].value == -192_007_000_000
    assert by_code["proceeds_from_borrowings"].raw_label == "Proceeds from loans"
    assert "Repayment of loans" == by_code["repayment_of_borrowings"].raw_label


def test_dataframe_parser_trims_generic_statement_prefixes_from_labels(db_session):
    _doc_with_artifact(
        db_session,
        [
            {
                **_table(
                    "balance_sheet",
                    [
                        {
                            "line": "Equity and liabilities Equity attributable to equity holders of the parent Share capital",
                            "2023Q4": "2,458",
                            "2022Q4": "2,458",
                        },
                    ],
                    extraction_method="text_table_fallback",
                ),
                "period": "2023Q4",
                "effective_period": "2023Q4",
                "comparative_period": "2022Q4",
                "columns": ["line", "2023Q4", "2022Q4"],
                "quality_flag": "raw_text_table",
                "warnings": ["text_table_fallback_used"],
            }
        ],
        ticker="X5_PREFIX",
        period="2023Q4",
    )

    result = DataFrameStatementParser(db_session, allow_text_fallback_semantic_gate=True).parse(
        "X5_PREFIX", "2023Q1", "2023Q4"
    )

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["share_capital"].raw_label == "Share capital"
    assert by_code["share_capital"].value == 2_458_000_000


def test_dataframe_parser_extracts_changes_in_equity_facts(db_session):
    _doc_with_artifact(
        db_session,
        [
            {
                **_table(
                    "changes_in_equity",
                    [
                        {"line": "Share capital", "2023": "2,458", "2022": "2,458"},
                        {"line": "Share premium", "2023": "24,510", "2022": "24,510"},
                        {"line": "Retained earnings", "2023": "101,250", "2022": "88,100"},
                        {"line": "Other reserves", "2023": "18,900", "2022": "17,450"},
                        {"line": "Non-controlling interests", "2023": "12,400", "2022": "10,900"},
                        {"line": "Total equity", "2023": "159,518", "2022": "143,418"},
                    ],
                ),
                "effective_period": "2023Q4",
                "comparative_period": "2022Q4",
                "period_confidence": 0.94,
            }
        ],
        ticker="X5_EQ",
        period="2023Q4",
    )

    result = DataFrameStatementParser(db_session).parse("X5_EQ", "2023Q1", "2023Q4")

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["share_capital"].value == 2_458_000_000
    assert by_code["share_premium"].value == 24_510_000_000
    assert by_code["retained_earnings"].value == 101_250_000_000
    assert by_code["other_reserves"].value == 18_900_000_000
    assert by_code["non_controlling_interests"].value == 12_400_000_000
    assert by_code["total_equity"].value == 159_518_000_000


def test_dataframe_parser_promotes_text_fallback_changes_in_equity_rows_through_unified_gate(db_session):
    _doc_with_artifact(
        db_session,
        [
            {
                **_table(
                    "changes_in_equity",
                    [
                        {"line": "Share capital 2,458 2,458"},
                        {"line": "Retained earnings 101,250 88,100"},
                        {"line": "Total equity 159,518 143,418"},
                    ],
                    extraction_method="text_table_fallback",
                ),
                "period": "2023Q4",
                "effective_period": "2023Q4",
                "comparative_period": "2022Q4",
                "period_confidence": 0.94,
                "columns": ["line", "2023Q4", "2022Q4"],
                "quality_flag": "raw_text_table",
                "warnings": ["text_table_fallback_used"],
            }
        ],
        ticker="X5_EQ_TF",
        period="2023Q4",
    )

    result = DataFrameStatementParser(db_session, allow_text_fallback_semantic_gate=True).parse(
        "X5_EQ_TF", "2023Q1", "2023Q4"
    )

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["share_capital"].value == 2_458_000_000
    assert by_code["retained_earnings"].value == 101_250_000_000
    assert by_code["total_equity"].value == 159_518_000_000
    assert "lower_trust_text_fallback_statement_row" in by_code["share_capital"].warnings


def test_banking_parser_quality_reports_primary_statement_coverage(db_session):
    _doc_with_artifact(
        db_session,
        [
            _bank_table(
                "balance_sheet",
                [
                    "Обобщенный консолидированный отчет о финансовом положении",
                    "в миллиардах российских рублей 2025 2024",
                    "Денежные средства и их эквиваленты 3938,0 2252,2",
                    "Кредиты и авансы клиентам 47972,7 43841,9",
                    "Средства физических лиц 33465,6 27821,6",
                    "Средства корпоративных клиентов 15907,9 16805,0",
                    "ИТОГО АКТИВОВ 68814,5 60855,1",
                    "ИТОГО ОБЯЗАТЕЛЬСТВ 60468,0 53681,6",
                    "ИТОГО СОБСТВЕННЫХ СРЕДСТВ 8346,5 7173,5",
                ],
                5,
            ),
            _bank_table(
                "income_statement",
                [
                    "Обобщенный консолидированный отчет о прибылях и убытках",
                    "в миллиардах российских рублей 2025 2024",
                    "Процентные доходы 4000,0 3500,0",
                    "Процентные расходы (444,0) (500,4)",
                    "Чистые процентные доходы 3556,0 2999,6",
                    "Комиссионные доходы 800,0 700,0",
                    "Комиссионные расходы (100,0) (90,0)",
                    "Операционные доходы 3487,4 3097,2",
                    "Расходы на содержание персонала и административные расходы (1237,4) (1062,6)",
                    "Прибыль до налогообложения 2250,0 2034,6",
                    "Прибыль за год 1705,9 1580,3",
                ],
                6,
            ),
            _bank_table(
                "cash_flow",
                [
                    "Обобщенный консолидированный отчет о движении денежных средств",
                    "в миллиардах российских рублей 2025 2024",
                    "Чистые денежные средства полученные от операционной деятельности 2996,6 1149,2",
                ],
                9,
            ),
        ],
        ticker="SBER",
        period="2025Q4",
    )

    result = DataFrameStatementParser(db_session, allow_text_fallback_semantic_gate=True).parse("SBER", "2025Q1", "2025Q4")
    quality = result.banking_parser_quality
    by_key = {(fact.metric_code, fact.period): fact for fact in result.facts}

    assert quality["primary_balance_sheet_found"] is True
    assert quality["primary_income_statement_found"] is True
    assert quality["primary_cash_flow_found"] is True
    assert quality["current_and_comparative_columns_found"] is True
    assert quality["notes_used"] is False
    assert quality["coverage_grade"] == "high", quality
    assert quality["accepted_bank_facts_count"] >= 10
    assert by_key[("cash_and_equivalents", "2025Q4")].value == 3_938_000_000_000
    assert "revenue" not in {fact.metric_code for fact in result.facts}


def test_banking_text_fallback_with_change_percent_column_uses_current_period_value(db_session):
    table = _bank_table(
        "income_statement",
        [],
        6,
    )
    table["company_ticker"] = "VTBR"
    table["rows"] = [
        {
            "line": "Процентные доходы, рассчитанные по методу эффективной процентной ставки 3 4 843,5 3",
            "2025Q4": "979,4",
            "2024Q4": "21,7%",
            "source_line": (
                "Процентные доходы, рассчитанные по методу эффективной процентной ставки "
                "3 4 843,5 3 979,4 21,7%"
            ),
            "inline_value_recovered": True,
        },
        {
            "line": "Чистые процентные доходы 3 433,6",
            "2025Q4": "487,2",
            "2024Q4": "-11,0%",
            "source_line": "Чистые процентные доходы 3 433,6 487,2 -11,0%",
            "inline_value_recovered": True,
        },
        {
            "line": "Чистая прибыль от изменения справедливой стоимости инвестиционной недвижимости 18 12,2",
            "2025Q4": "7,0",
            "2024Q4": "74,3%",
            "source_line": "Чистая прибыль от изменения справедливой стоимости инвестиционной недвижимости 18 12,2 7,0 74,3%",
            "inline_value_recovered": True,
        },
        {
            "line": "Чистая прибыль 502,1",
            "2025Q4": "551,4",
            "2024Q4": "-8,9%",
            "source_line": "Чистая прибыль 502,1 551,4 -8,9%",
            "inline_value_recovered": True,
        },
    ]
    table["row_blocks"] = [
        {
            "label_text": row["line"],
            "value_cells": {key: value for key, value in row.items() if key in {"2025Q4", "2024Q4"}},
            "row_kind": "statement_line_item",
            "row_confidence": 0.84,
            "label_confidence": 0.86,
            "value_confidence": 0.86,
            "ownership_confidence": 0.9,
            "fact_period_confidence": 0.9,
            "source_engine": "native_pdf_table_engine",
            "source_page": 6,
            "source_table_id": "test-bank-change-column",
            "fusion_status": "single_engine",
            "source_engines_involved": ["native_pdf_table_engine"],
            "diagnostics": {"source_line": row["source_line"]},
        }
        for row in table["rows"]
    ]
    table["dataframe_json"] = {"orientation": "records", "data": table["rows"]}
    _doc_with_artifact(db_session, [table], ticker="VTBR", period="2025Q4")

    result = DataFrameStatementParser(db_session, allow_text_fallback_semantic_gate=True).parse("VTBR", "2025Q1", "2025Q4")
    by_code = {fact.metric_code: fact for fact in result.facts}
    debug = {
        "facts": [(fact.metric_code, fact.raw_label, fact.value) for fact in result.facts],
        "rejected": [(item.raw_label, item.metric_code, item.reason) for item in result.rejected_candidates],
        "numeric_evidence": [(item.get("raw_label"), item.get("reasons")) for item in result.unmapped_numeric_evidence],
    }

    assert "interest_income" in by_code, debug
    assert by_code["interest_income"].value == 4_843_500_000_000
    assert by_code["net_interest_income"].value == 433_600_000_000
    assert by_code["net_income"].value == 502_100_000_000
    assert all("справедливой стоимости инвестиционной недвижимости" not in fact.raw_label for fact in result.facts)


def test_dataframe_parser_uses_company_sector_to_route_unknown_bank_ticker(db_session):
    company = Company(
        ticker="BANK1",
        board="TQBR",
        short_name="Example Bank",
        full_name="Example Bank PJSC",
        aliases_json=[],
        sector="financials",
        subsector="bank",
    )
    db_session.add(company)
    db_session.flush()
    _doc_with_artifact(
        db_session,
        [
            _table(
                "income_statement",
                [{"line": "Interest income", "2021": "100", "2020": "90"}],
            )
        ],
        ticker="BANK1",
        period="2021Q4",
    )

    result = DataFrameStatementParser(db_session).parse("BANK1", "2021Q1", "2021Q4")

    assert {fact.metric_code for fact in result.facts} == {"interest_income"}
    assert result.facts[0].source_location["raw_label"] == "Interest income"
    assert not any(item.reason == "banking_concept_not_industrial_metric" for item in result.rejected_candidates)
    assert result.banking_parser_quality["primary_balance_sheet_found"] is False


def test_banking_balance_sheet_aliases_support_ru_primary_statement_rows(db_session):
    _doc_with_artifact(
        db_session,
        [
            _table(
                "balance_sheet",
                [
                    {"line": "Денежные средства и их эквиваленты", "2021": "10", "2020": "8"},
                    {"line": "Кредиты и авансы клиентам", "2021": "100", "2020": "90"},
                    {"line": "Средства клиентов", "2021": "80", "2020": "70"},
                    {"line": "Итого активов", "2021": "150", "2020": "130"},
                    {"line": "Итого обязательств", "2021": "120", "2020": "105"},
                    {"line": "Итого собственных средств", "2021": "30", "2020": "25"},
                ],
            )
        ],
        ticker="VTBR",
        period="2021Q4",
    )

    result = DataFrameStatementParser(db_session).parse("VTBR", "2021Q1", "2021Q4")

    assert {
        "cash_and_equivalents",
        "loans_to_customers",
        "customer_accounts",
        "total_assets",
        "total_liabilities",
        "total_equity",
    } <= {fact.metric_code for fact in result.facts}


def test_banking_customer_accounts_derivation_requires_retail_and_corporate_components(db_session):
    parser = DataFrameStatementParser(db_session)
    base = {
        "company_ticker": "SBER",
        "period": "2025Q4",
        "reporting_standard": "IFRS",
        "currency": "RUB",
        "unit_multiplier": 1.0,
        "period_type": "balance_sheet_snapshot",
        "source_document_id": 1,
        "source_table_type": "balance_sheet",
        "source_table_index": 0,
        "confidence_score": 0.94,
        "quality_flag": "high_confidence_text_fallback",
        "extraction_method": "text_table_fallback_semantic_gate",
    }
    retail = StatementFactCandidate(
        **base,
        metric_code="retail_customer_accounts",
        value=33_465_600_000_000,
        source_location={"raw_label": "Средства физических лиц"},
        raw_label="Средства физических лиц",
        raw_value="33465,6",
    )
    corporate = StatementFactCandidate(
        **base,
        metric_code="corporate_customer_accounts",
        value=15_907_900_000_000,
        source_location={"raw_label": "Средства корпоративных клиентов"},
        raw_label="Средства корпоративных клиентов",
        raw_value="15907,9",
    )

    derived = parser._derived_banking_facts([retail, corporate])

    assert len(derived) == 1
    assert derived[0].metric_code == "customer_accounts"
    assert derived[0].value == 49_373_500_000_000
    assert derived[0].source_location["formula"] == "retail_customer_accounts + corporate_customer_accounts"


def test_dataframe_parser_rejects_ambiguous_period_column(db_session):
    _doc_with_artifact(
        db_session,
        [_table("income_statement", [{"line": "Revenue", "For 2021": "100", "FY 2021": "99"}])],
    )

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4")

    assert result.facts == []
    assert result.rejected_candidates[0].reason == "ambiguous_period_column"


def test_dataframe_parser_rejects_missing_source_location(db_session):
    _doc_with_artifact(
        db_session,
        [_table("income_statement", [{"line": "Revenue", "2021": "100"}], source_location=None)],
    )
    path = statement_tables_path(db_session.scalars(select(ReportDocument)).all()[-1])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["statement_tables"][0]["source_location"] = None
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4")

    assert result.facts == []
    assert result.rejected_candidates[0].reason == "missing_traceability"


def test_parse_number_handles_parentheses_and_separators():
    assert parse_number("(1,234)") == -1234
    assert parse_number("1 234") == 1234
    assert parse_number("1,5") == 1.5


def test_dataframe_parser_selects_current_period_for_non_2021_quarters(db_session):
    _doc_with_artifact(
        db_session,
        [
            {
                "document_id": 1,
                "company_ticker": "LKOH",
                "period": "2026Q4",
                "reporting_standard": "IFRS",
                "statement_type": "income_statement",
                "period_type": "annual",
                "table_index": 0,
                "page_number": 1,
                "table_title": "income_statement",
                "unit": "million",
                "currency": "RUB",
                "columns": ["line", "FY 2026", "FY 2025"],
                "rows": [{"line": "Revenue", "FY 2026": "100", "FY 2025": "90"}],
                "dataframe_json": {"orientation": "records", "data": [{"line": "Revenue", "FY 2026": "100", "FY 2025": "90"}]},
                "source_location": {"page": 1, "table_index": 0},
                "confidence_score": 0.85,
                "extraction_method": "pdf_table",
                "quality_flag": "raw_table",
                "warnings": [],
            }
        ],
        period="2026Q4",
    )

    result = DataFrameStatementParser(db_session).parse("LKOH", "2026Q1", "2026Q4")

    assert len(result.facts) == 1
    assert result.facts[0].metric_code == "revenue"
    assert result.facts[0].value == 100_000_000
    assert result.facts[0].source_location["column_name"] == "FY 2026"


def test_dataframe_parser_uses_effective_period_for_plain_year_columns(db_session):
    ticker = "PLNY"
    year_2025 = "2025"
    year_2024 = "2024"
    _doc_with_artifact(
        db_session,
        [
            {
                "document_id": 1,
                "company_ticker": ticker,
                "period": "2026Q4",
                "effective_period": "2025Q4",
                "comparative_period": "2024Q4",
                "period_source": "table_headers",
                "period_confidence": 0.95,
                "reporting_standard": "IFRS",
                "statement_type": "income_statement",
                "period_type": "annual",
                "table_index": 0,
                "page_number": 1,
                "table_title": "income_statement",
                "unit": "million",
                "currency": "RUB",
                "columns": ["line", year_2025, year_2024],
                "rows": [{"line": "Revenue", year_2025: "100", year_2024: "90"}],
                "dataframe_json": {"orientation": "records", "data": [{"line": "Revenue", year_2025: "100", year_2024: "90"}]},
                "source_location": {"page": 1, "table_index": 0},
                "confidence_score": 0.85,
                "extraction_method": "pdf_table",
                "quality_flag": "raw_table",
                "warnings": [],
            }
        ],
        ticker=ticker,
        period="2026Q4",
    )

    result = DataFrameStatementParser(db_session).parse(ticker, "2025Q1", "2025Q4")

    assert len(result.facts) == 1
    assert result.facts[0].metric_code == "revenue"
    assert result.facts[0].period == "2025Q4"
    assert result.facts[0].source_location["column_name"] == year_2025
    assert result.period_resolution["effective_report_period"] == "2025Q4"


def test_industrial_operating_expenses_is_not_sector_blocked():
    metric, reason = INCOME_STATEMENT_INTERPRETER.interpret(
        StatementRowInterpretationContext(
            raw_label="Operating expenses",
            row_kind="statement_line_item",
            issuer_class="industrial_ifrs_retail",
        )
    )

    assert metric == "operating_expenses"
    assert reason is None
