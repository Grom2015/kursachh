import json

from sqlalchemy import select

from app.db.models import Company, ReportDocument
from app.services.parsing.dataframe_statement_parser import (
    DataFrameStatementParser,
    StatementFactCandidate,
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
    assert by_code["revenue"].source_location["column_name"] == "2021"


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

    result = DataFrameStatementParser(db_session, allow_text_fallback_semantic_gate=True).parse(
        "SBER", "2025Q1", "2025Q4"
    )
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
