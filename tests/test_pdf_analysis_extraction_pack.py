import json

from sqlalchemy import select

from app.db.models import Company, ReportDocument
from app.services.parsing.dataframe_statement_parser import DataFrameStatementParser
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
            sector="financials" if ticker in {"SBER", "MOEX"} else "oil_gas",
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
        source_type="manual_upload",
        source_url="https://example.com/report.pdf",
        status="validated",
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


def _table(statement_type, rows, *, extraction_method="pdf_table", title=None, page_number=1):
    return {
        "document_id": 1,
        "company_ticker": "LKOH",
        "period": "2021Q4",
        "reporting_standard": "IFRS",
        "statement_type": statement_type,
        "period_type": "annual",
        "table_index": page_number,
        "page_number": page_number,
        "table_title": title or statement_type,
        "unit": "million",
        "currency": "RUB",
        "columns": ["line", "2021", "2020"],
        "rows": rows,
        "dataframe_json": {"orientation": "records", "data": rows},
        "source_location": {"page": page_number, "table_index": page_number},
        "confidence_score": 0.85,
        "extraction_method": extraction_method,
        "quality_flag": "raw_table",
        "warnings": [],
    }


def test_v2_structured_facts_and_unmapped_numeric_evidence_are_separated(db_session):
    _doc_with_artifact(
        db_session,
        [
            _table(
                "income_statement",
                [
                    {"line": "Revenue", "2021": "100", "2020": "90"},
                    {"line": "Operating profit", "2021": "20", "2020": "10"},
                    {"line": "Unknown operating line", "2021": "7", "2020": "5"},
                ],
            )
        ],
    )

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4").to_dict()

    assert {fact["metric_code"] for fact in result["structured_facts"]} >= {"revenue", "operating_profit"}
    assert len(result["unmapped_numeric_evidence"]) >= 1
    assert any(item["raw_label"] == "Unknown operating line" for item in result["unmapped_numeric_evidence"])
    assert all(item["not_confirmed_fact"] is True for item in result["unmapped_numeric_evidence"])
    assert all(item["not_for_ratio_calculation"] is True for item in result["unmapped_numeric_evidence"])
    assert "row_parsing_diagnostics" in result
    assert "statement_family_summary" in result
    assert "fact_confidence_summary" in result


def test_v2_rejected_rows_preserve_reasons_and_total_debt_not_derived(db_session):
    _doc_with_artifact(
        db_session,
        [
            _table(
                "balance_sheet",
                [
                    {"line": "Short term borrowings", "2021": "10", "2020": "9"},
                    {"line": "Long term borrowings", "2021": "25", "2020": "20"},
                ],
            )
        ],
    )

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4").to_dict()
    reasons = {item["rejection_reason"] for item in result["rejected_rows"]}

    assert "debt_component_not_total_debt" in reasons
    assert "total_debt" not in {fact["metric_code"] for fact in result["structured_facts"]}


def test_v2_unmapped_table_evidence_and_full_llm_pack_are_retained(db_session):
    _doc_with_artifact(
        db_session,
        [
            _table(
                "note",
                [
                    {"line": "Note 12 segment revenue", "2021": "200", "2020": "180"},
                    {"line": "Note 12 segment EBITDA", "2021": "40", "2020": "35"},
                ],
                title="Примечание 12. Сегменты",
                page_number=12,
            )
        ],
    )

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4").to_dict()
    llm_pack = result["llm_ready_evidence_pack"]

    assert len(result["unmapped_table_evidence"]) == 1
    assert result["unmapped_table_evidence"][0]["note_classification"] == "review_only_note"
    assert result["unmapped_table_evidence"][0]["not_confirmed_fact"] is True
    assert llm_pack["unresolved_table_evidence"] == result["unmapped_table_evidence"]
    assert "Do not calculate ratios from unresolved evidence." in llm_pack["instructions"]


def test_row_kind_gates_prevent_component_current_assets_promotion(db_session):
    _doc_with_artifact(
        db_session,
        [
            _table(
                "balance_sheet",
                [
                    {"line": "Other current assets", "2021": "20", "2020": "15"},
                    {"line": "Total assets", "2021": "150", "2020": "120"},
                ],
            )
        ],
    )

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4").to_dict()

    assert "current_assets" not in {fact["metric_code"] for fact in result["structured_facts"]}
    assert "total_assets" in {fact["metric_code"] for fact in result["structured_facts"]}


def test_v2_banking_and_industrial_concepts_do_not_mix(db_session):
    _doc_with_artifact(
        db_session,
        [
            _table(
                "income_statement",
                [{"line": "Revenue", "2021": "100", "2020": "90"}],
            )
        ],
        ticker="SBER",
        period="2021Q4",
    )

    result = DataFrameStatementParser(db_session).parse("SBER", "2021Q1", "2021Q4").to_dict()

    assert result["structured_facts"] == []
    assert any(item["rejection_reason"] == "industrial_concept_not_banking_metric" for item in result["rejected_rows"])


def test_balance_sheet_total_equity_plus_liabilities_line_is_not_promoted_to_total_equity(db_session):
    _doc_with_artifact(
        db_session,
        [
            _table(
                "balance_sheet",
                [{"line": "Итого капитал и обязательства", "2021": "1000", "2020": "900"}],
            )
        ],
        period="2021Q4",
    )

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4").to_dict()

    assert "total_equity" not in {fact["metric_code"] for fact in result["structured_facts"]}
    assert any(item["raw_label"] == "Итого капитал и обязательства" for item in result["unmapped_numeric_evidence"])
