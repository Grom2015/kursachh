import json

from sqlalchemy import select

from app.db.models import Company, ReportDocument, StatementFact
from app.services.parsing.dataframe_statement_parser import DataFrameStatementParser
from app.services.parsing.statement_table_extractor import statement_tables_path
from app.services.parsing.text_fallback_semantic_gate import evaluate_text_fallback_table
from app.tools import parse_statement_tables_to_facts as cli


class SessionContext:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *_args):
        return None


def _doc_with_artifact(db_session, tables):
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
    normalized_tables = []
    for table in tables:
        normalized = {**table, "document_id": doc.id}
        normalized_tables.append(normalized)
    path = statement_tables_path(doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"document_id": doc.id, "period": "2021Q4", "statement_tables": normalized_tables}),
        encoding="utf-8",
    )
    return doc


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


def test_default_parser_still_rejects_text_fallback(db_session):
    _doc_with_artifact(
        db_session,
        [
            _fallback_table(
                "balance_sheet",
                "Consolidated Statement of Financial Position",
                [
                    "Consolidated Statement of Financial Position",
                    "Note 31 December 2021 31 December 2020",
                    "Total assets 500 400",
                ],
            )
        ],
    )

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4")

    assert result.facts == []
    assert result.rejected_candidates[0].reason == "text_table_fallback_not_eligible_for_fact_normalization"


def test_explicit_gate_extracts_primary_balance_sheet_row(db_session):
    _doc_with_artifact(
        db_session,
        [
            _fallback_table(
                "balance_sheet",
                "Consolidated Statement of Financial Position",
                [
                    "Consolidated Statement of Financial Position",
                    "(Millions of Russian rubles)",
                    "Note 31 December 2021 31 December 2020",
                    "Cash and cash equivalents 6 677,482 343,832",
                    "Total assets 6,864,749 5,991,579",
                ],
            )
        ],
    )

    result = DataFrameStatementParser(db_session, allow_text_fallback_semantic_gate=True).parse(
        "LKOH", "2021Q1", "2021Q4"
    )

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["cash_and_equivalents"].value == 677_482_000_000
    assert by_code["cash_and_equivalents"].extraction_method == "text_table_fallback_semantic_gate"


def test_sber_russian_bank_primary_fallback_extracts_safe_facts(db_session):
    company = Company(ticker="SBER", isin="RU0009029540", board="TQBR", short_name="SBER", full_name="Sberbank")
    db_session.add(company)
    db_session.flush()
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2025",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="manual_upload",
        source_url=None,
        status="validated",
    )
    db_session.add(doc)
    db_session.flush()
    tables = [
            {
                **_fallback_table(
                    "balance_sheet",
                    "Обобщенный консолидированный отчет о финансовом положении",
                    [
                        "31 декабря 31 декабря",
                        "в миллиардах российских рублей Прим. 2025 года 2024 года",
                        "Денежные средства и их эквиваленты 5 3 938,0 2 252,2",
                        "ИТОГО АКТИВОВ 68 814,5 60 855,1",
                        "ИТОГО СОБСТВЕННЫХ СРЕДСТВ 8 346,5 7 173,5",
                    ],
                ),
                "period": "2025",
                "unit": "billion",
                "unit_multiplier": 1_000_000_000,
            },
            {
                **_fallback_table(
                    "income_statement",
                    "Обобщенный консолидированный отчет о прибылях и убытках",
                    [
                        "За год, закончившийся 31 декабря",
                        "в миллиардах российских рублей Прим. 2025 года 2024 года",
                        "Прибыль за год 1 705,9 1 580,3",
                    ],
                    table_index=1,
                ),
                "period": "2025",
                "unit": "billion",
                "unit_multiplier": 1_000_000_000,
            },
            {
                **_fallback_table(
                    "cash_flow",
                    "Обобщенный консолидированный отчет о движении денежных средств",
                    [
                        "За год, закончившийся 31 декабря",
                        "в миллиардах российских рублей Прим. 2025 года 2024 года",
                        "Чистые денежные средства, полученные от операционной деятельности 2 996,6 1 149,2",
                    ],
                    table_index=2,
                ),
                "period": "2025",
                "unit": "billion",
                "unit_multiplier": 1_000_000_000,
            },
        ]
    for table in tables:
        table["document_id"] = doc.id
    path = statement_tables_path(doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"document_id": doc.id, "period": "2025", "statement_tables": tables}), encoding="utf-8")

    result = DataFrameStatementParser(db_session, allow_text_fallback_semantic_gate=True).parse(
        "SBER", "2025Q1", "2025Q4"
    )

    by_code = {fact.metric_code: fact for fact in result.facts}
    assert by_code["cash_and_equivalents"].period == "2025Q4"
    assert by_code["cash_and_equivalents"].value == 3_938_000_000_000
    assert by_code["total_assets"].value == 68_814_500_000_000
    assert by_code["total_equity"].value == 8_346_500_000_000
    assert by_code["net_income"].value == 1_705_900_000_000
    assert by_code["operating_cash_flow"].value == 2_996_600_000_000
    assert "revenue" not in by_code
    assert "operating_profit" not in by_code
    assert by_code["cash_and_equivalents"].quality_flag == "high_confidence_text_fallback"
    assert by_code["cash_and_equivalents"].source_location["source_line"].startswith("Денежные средства")
    assert "derived_from_raw_text_table_requires_extra_caution" in by_code["cash_and_equivalents"].warnings


def test_gazp_like_balance_sheet_rows_extract_expected_assets_and_liabilities():
    table = _fallback_table(
        "balance_sheet",
        "Consolidated Balance Sheet",
        [
            "Consolidated Balance Sheet",
            "(in millions of Russian Rubles)",
            "31 December 2021 31 December 2020",
            "Current assets 8,252,330 6,214,791",
            "Current liabilities 5,875,908 4,423,165",
            "Total equity 14,982,643 12,701,644",
        ],
    )
    result = evaluate_text_fallback_table(table, "IFRS", "GAZP", "2021Q4", "balance_sheet")
    by_code = {row.metric_code: row for row in result.accepted_rows}

    assert by_code["current_assets"].value == 8_252_330_000_000
    assert by_code["current_liabilities"].value == 5_875_908_000_000
    assert by_code["total_equity"].value == 14_982_643_000_000


def test_bank_income_labels_are_sector_safe_not_industrial_revenue():
    table = _fallback_table(
        "income_statement",
        "Обобщенный консолидированный отчет о прибылях и убытках",
        [
            "Обобщенный консолидированный отчет о прибылях и убытках",
            "в миллиардах российских рублей 2025 2024",
            "Процентные доходы 4 000,0 3 500,0",
            "Чистые процентные доходы 2 000,0 1 900,0",
            "Операционные расходы (800,0) (700,0)",
        ],
    )
    table["unit"] = "billion"
    table["unit_multiplier"] = 1_000_000_000

    result = evaluate_text_fallback_table(table, "IFRS", "SBER", "2025Q4", "income_statement")
    by_code = {row.metric_code: row for row in result.accepted_rows}

    assert "revenue" not in by_code
    assert by_code["interest_income"].value == 4_000_000_000_000
    assert by_code["net_interest_income"].value == 2_000_000_000_000
    assert by_code["operating_expenses"].value == -800_000_000_000
    assert not any(row.metric_code == "revenue" for row in result.accepted_rows)


def test_bank_primary_rows_keep_current_and_comparative_columns():
    table = _fallback_table(
        "balance_sheet",
        "Обобщенный консолидированный отчет о финансовом положении",
        [
            "Обобщенный консолидированный отчет о финансовом положении",
            "в миллиардах российских рублей 2025 2024",
            "ИТОГО АКТИВОВ 68 814,5 60 855,1",
            "Средства физических лиц 33 465,6 27 821,6",
            "Средства корпоративных клиентов 15 907,9 16 805,0",
        ],
    )
    table["unit"] = "billion"
    table["unit_multiplier"] = 1_000_000_000

    result = evaluate_text_fallback_table(table, "IFRS", "SBER", "2025Q4", "balance_sheet")
    by_key = {(row.metric_code, row.period): row for row in result.accepted_rows}

    assert by_key[("total_assets", "2025Q4")].value == 68_814_500_000_000
    assert by_key[("total_assets", "2024Q4")].value == 60_855_100_000_000
    assert by_key[("retail_customer_accounts", "2025Q4")].value == 33_465_600_000_000
    assert by_key[("corporate_customer_accounts", "2024Q4")].value == 16_805_000_000_000


def test_cash_flow_gate_extracts_operating_cash_flow():
    table = _fallback_table(
        "cash_flow",
        "Consolidated Statement of Cash Flows",
        [
            "Consolidated Statement of Cash Flows",
            "Note 2021 2020",
            "Cash flows from operating activities",
            "Net cash provided by operating activities 1,126,614 776,574",
        ],
    )

    result = evaluate_text_fallback_table(table, "IFRS", "LKOH", "2021Q4", "cash_flow")

    assert result.accepted_rows[0].metric_code == "operating_cash_flow"
    assert result.accepted_rows[0].value == 1_126_614_000_000


def test_income_gate_extracts_revenue_from_sales_and_other_operating_revenues():
    table = _fallback_table(
        "income_statement",
        "Consolidated Statement of Profit or Loss and Other Comprehensive Income",
        [
            "Consolidated Statement of Profit or Loss and Other Comprehensive Income",
            "Note 2021 2020",
            "Sales and other operating revenues 2 285 161 1 934 320",
        ],
    )

    result = evaluate_text_fallback_table(table, "IFRS", "LKOH", "2021Q4", "income_statement")

    assert result.accepted_rows[0].metric_code == "revenue"
    assert result.accepted_rows[0].value == 2_285_161_000_000


def test_income_gate_extracts_net_income_and_operating_profit():
    table = _fallback_table(
        "income_statement",
        "Consolidated Statement of Profit or Loss and Other Comprehensive Income",
        [
            "Consolidated Statement of Profit or Loss and Other Comprehensive Income",
            "Note 2021 2020",
            "Profit from operating activities 200 100",
            "Profit for the year 157 414 46 292",
        ],
    )

    result = evaluate_text_fallback_table(table, "IFRS", "LKOH", "2021Q4", "income_statement")
    by_code = {row.metric_code: row for row in result.accepted_rows}

    assert by_code["operating_profit"].value == 200_000_000
    assert by_code["net_income"].value == 157_414_000_000


def test_gazp_like_income_statement_rows_extract_net_income_and_operating_profit():
    table = _fallback_table(
        "income_statement",
        "Consolidated Statement of Comprehensive Income",
        [
            "Consolidated Statement of Comprehensive Income",
            "(in millions of Russian Rubles)",
            "Year ended 31 December 2021 2020",
            "Operating profit 2,515,430 706,629",
            "Profit for the year 2,093,167 162,414",
        ],
    )

    result = evaluate_text_fallback_table(table, "IFRS", "GAZP", "2021Q4", "income_statement")
    by_code = {row.metric_code: row for row in result.accepted_rows}

    assert by_code["operating_profit"].value == 2_515_430_000_000
    assert by_code["net_income"].value == 2_093_167_000_000


def test_income_gate_reconstructs_adjacent_split_lines():
    table = _fallback_table(
        "income_statement",
        "Consolidated Statement of Profit or Loss and Other Comprehensive Income",
        [
            "Consolidated Statement of Profit or Loss and Other Comprehensive Income",
            "Note 2021 2020",
            "Sales and other operating",
            "revenues 100 90",
        ],
    )

    result = evaluate_text_fallback_table(table, "IFRS", "LKOH", "2021Q4", "income_statement")

    assert result.accepted_rows[0].metric_code == "revenue"
    assert result.accepted_rows[0].source_lines == ["Sales and other operating", "revenues 100 90"]


def test_income_gate_inferred_current_column_adds_warning():
    table = _fallback_table(
        "income_statement",
        "Consolidated Statement of Profit or Loss and Other Comprehensive Income",
        [
            "Consolidated Statement of Profit or Loss and Other Comprehensive Income",
            "(Millions of Russian rubles)",
            "Revenue 100 90",
        ],
    )

    result = evaluate_text_fallback_table(table, "IFRS", "LKOH", "2021Q4", "income_statement")

    assert result.accepted_rows[0].metric_code == "revenue"
    assert "current_period_column_inferred_from_statement_layout" in result.accepted_rows[0].warnings
    assert result.accepted_rows[0].source_location["metric_input_allowed_by_default"] is False
    assert result.accepted_rows[0].source_location["requires_downstream_quality_gate"] is True


def test_income_gate_selects_ytd_column_for_interim_four_column_statement():
    table = _fallback_table(
        "income_statement",
        "Consolidated Statement of Profit or Loss and Other Comprehensive Income",
        [
            "Consolidated Statement of Profit or Loss and Other Comprehensive Income",
            "For the three For the three For the six For the six",
            "months ended months ended months ended months ended",
            "30 June 2021 30 June 2020 30 June 2021 30 June 2020",
            "Sales and other operating revenues 27 2,201,884 986,427 4,078,367 2,652,412",
        ],
    )

    result = evaluate_text_fallback_table(table, "IFRS", "LKOH", "2021Q2", "income_statement")

    assert result.accepted_rows[0].metric_code == "revenue"
    assert result.accepted_rows[0].value == 4_078_367_000_000
    assert result.accepted_rows[0].column_name == "current_ytd_third"


def test_income_gate_rejects_per_share_rows():
    table = _fallback_table(
        "income_statement",
        "Consolidated Statement of Profit or Loss and Other Comprehensive Income",
        [
            "Consolidated Statement of Profit or Loss and Other Comprehensive Income",
            "Note 2021 2020",
            "Profit for the period attributable to PJSC LUKOIL shareholders per share of common stock 20",
        ],
    )

    result = evaluate_text_fallback_table(table, "IFRS", "LKOH", "2021Q4", "income_statement")

    assert result.accepted_rows == []
    assert result.rejected_rows[0].reason == "per_share_row_not_statement_fact"


def test_notes_like_fallback_is_rejected(db_session):
    _doc_with_artifact(
        db_session,
        [
            _fallback_table(
                "income_statement",
                "Notes to Consolidated Financial Statements",
                [
                    "Notes to Consolidated Financial Statements",
                    "Note 28. Income tax",
                    "Total income tax expense 191,451 82,154",
                ],
            )
        ],
    )

    result = DataFrameStatementParser(db_session, allow_text_fallback_semantic_gate=True).parse(
        "LKOH", "2021Q1", "2021Q4"
    )

    assert result.facts == []
    assert result.rejected_candidates[0].reason == "notes_fallback_not_eligible_for_fact_normalization"


def test_ambiguous_multi_number_row_is_rejected():
    table = _fallback_table(
        "income_statement",
        "Consolidated Statement of Profit or Loss and Other Comprehensive Income",
        [
            "Consolidated Statement of Profit or Loss and Other Comprehensive Income",
            "For the three months and year ended 2021 2020",
            "Revenue 10 20 30 40",
        ],
    )

    result = evaluate_text_fallback_table(table, "IFRS", "LKOH", "2021Q4", "income_statement")

    assert result.accepted_rows == []
    assert result.rejected_rows[0].reason == "ambiguous_period_column"


def test_missing_source_location_rejects_table():
    table = _fallback_table(
        "balance_sheet",
        "Consolidated Statement of Financial Position",
        ["Consolidated Statement of Financial Position", "Note 31 December 2021 31 December 2020", "Total assets 500 400"],
        source_location=None,
    )
    table["source_location"] = None

    result = evaluate_text_fallback_table(table, "IFRS", "LKOH", "2021Q4", "balance_sheet")

    assert result.accepted_rows == []
    assert result.rejected_rows[0].reason == "missing_traceability"


def test_ebitda_and_debt_are_rejected_in_fallback_stage():
    table = _fallback_table(
        "balance_sheet",
        "Consolidated Statement of Financial Position",
        [
            "Consolidated Statement of Financial Position",
            "Note 31 December 2021 31 December 2020",
            "Total debt 100 90",
            "EBITDA 50 40",
        ],
    )

    result = evaluate_text_fallback_table(table, "IFRS", "LKOH", "2021Q4", "balance_sheet")

    assert result.accepted_rows == []
    assert {row.reason for row in result.rejected_rows} == {"unsupported_metric_for_text_fallback"}


def test_cli_gate_does_not_mutate_without_persist(db_session, monkeypatch):
    _doc_with_artifact(
        db_session,
        [
            _fallback_table(
                "balance_sheet",
                "Consolidated Statement of Financial Position",
                [
                    "Consolidated Statement of Financial Position",
                    "Note 31 December 2021 31 December 2020",
                    "Total assets 500 400",
                ],
            )
        ],
    )
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "SessionLocal", lambda: SessionContext(db_session))
    before = db_session.query(StatementFact).count()

    report = cli.parse_statement_tables_to_facts(
        "LKOH",
        "2021Q1",
        "2021Q4",
        replay_cache=True,
        persist=False,
        allow_text_fallback_semantic_gate=True,
    )

    assert report["canonical_facts_created"] == 1
    assert report["text_fallback_semantic_gate_enabled"] is True
    assert report["text_fallback_facts_created"] == 1
    assert report["db_persisted"] is False
    assert db_session.query(StatementFact).count() == before
