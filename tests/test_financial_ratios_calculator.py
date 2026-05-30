import json
import shutil
import uuid
from pathlib import Path

import pytest

from app.db.models import Company, ReportDocument, StatementFact
from app.services.metrics.financial_ratios_calculator import (
    FinancialRatiosCalculator,
    FinancialRatiosRequest,
)


def _company(db_session, ticker: str = "TEST") -> Company:
    company = Company(ticker=ticker, board="TQBR", short_name=ticker, full_name=ticker, aliases_json=[])
    db_session.add(company)
    db_session.commit()
    return company


def _doc(db_session, company: Company, source_type: str = "issuer_ir_manifest") -> ReportDocument:
    doc = ReportDocument(
        company_id=company.id,
        report_period="2021Q4",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type=source_type,
        storage_path="dummy.pdf",
        file_name="dummy.pdf",
        file_hash=str(uuid.uuid4()),
        status="parsed",
    )
    db_session.add(doc)
    db_session.commit()
    return doc


def _fact(
    db_session,
    company: Company,
    doc: ReportDocument,
    metric_code: str,
    value: float,
    period: str = "2021Q4",
    period_type: str = "annual",
    standard: str = "IFRS",
    quality_flag: str = "exact",
    unit_multiplier: float = 1.0,
    source_location: dict | None = None,
) -> StatementFact:
    fact = StatementFact(
        company_id=company.id,
        report_document_id=doc.id,
        period=period,
        reporting_standard=standard,
        statement_type="statement",
        metric_code=metric_code,
        metric_name_original=metric_code,
        value=value,
        currency="RUB",
        unit_multiplier=unit_multiplier,
        period_type=period_type,
        source_location=source_location or {"raw_label": metric_code, "source_role": "financial_statements"},
        quality_flag=quality_flag,
        confidence_score=0.95,
    )
    db_session.add(fact)
    db_session.commit()
    return fact


def _calculate(db_session, company: Company, **kwargs) -> dict:
    report = FinancialRatiosCalculator(db=db_session).calculate(
        FinancialRatiosRequest(
            company_ticker=company.ticker,
            period_from=kwargs.pop("period_from", "2021Q4"),
            period_to=kwargs.pop("period_to", "2021Q4"),
            reporting_standard=kwargs.pop("reporting_standard", "IFRS"),
            **kwargs,
        )
    )
    return report.to_dict()


def _metric(report: dict, code: str, period: str = "2021Q4") -> dict:
    return next(item for item in report["metrics"] if item["metric_code"] == code and item["period"] == period)


def test_core_ratio_formulas(db_session):
    company = _company(db_session)
    doc = _doc(db_session, company)
    for code, value, period_type in [
        ("revenue", 100.0, "annual"),
        ("operating_profit", 20.0, "annual"),
        ("net_income", 10.0, "annual"),
        ("current_assets", 50.0, "balance_sheet_snapshot"),
        ("current_liabilities", 25.0, "balance_sheet_snapshot"),
        ("total_debt", 30.0, "balance_sheet_snapshot"),
        ("total_equity", 60.0, "balance_sheet_snapshot"),
    ]:
        _fact(db_session, company, doc, code, value, period_type=period_type)

    report = _calculate(db_session, company)

    assert _metric(report, "operating_margin")["value"] == 0.2
    assert _metric(report, "net_margin")["value"] == 0.1
    assert _metric(report, "current_ratio")["value"] == 2.0
    assert _metric(report, "debt_to_equity")["value"] == 0.5


def test_fcf_positive_and_negative_capex(db_session):
    company = _company(db_session)
    doc = _doc(db_session, company)
    _fact(db_session, company, doc, "operating_cash_flow", 100.0, period_type="annual")
    _fact(db_session, company, doc, "capex", 30.0, period_type="annual")
    _fact(db_session, company, doc, "revenue", 200.0, period_type="annual")
    positive = _calculate(db_session, company)
    assert _metric(positive, "fcf")["value"] == 70.0
    assert _metric(positive, "fcf_margin")["value"] == 0.35

    company2 = _company(db_session, "NEG")
    doc2 = _doc(db_session, company2)
    _fact(db_session, company2, doc2, "operating_cash_flow", 100.0, period_type="annual")
    _fact(db_session, company2, doc2, "capex", -30.0, period_type="annual")
    negative = _calculate(db_session, company2)
    assert _metric(negative, "fcf")["value"] == 70.0


def test_ebitda_margin_requires_explicit_ebitda_and_no_proxy(db_session):
    company = _company(db_session)
    doc = _doc(db_session, company)
    _fact(db_session, company, doc, "revenue", 100.0)
    _fact(db_session, company, doc, "operating_profit", 20.0)

    report = _calculate(db_session, company)
    metric = _metric(report, "ebitda_margin")

    assert metric["status"] == "unsupported_by_policy"
    assert metric["reason"] == "explicit_ebitda_missing_no_proxy_allowed"
    assert metric["inputs_missing"] == ["ebitda"]


def test_roe_roa_require_previous_snapshot_and_can_use_outside_range(db_session):
    company = _company(db_session)
    doc = _doc(db_session, company)
    _fact(db_session, company, doc, "net_income", 20.0, period_type="annual")
    _fact(db_session, company, doc, "total_equity", 120.0, period_type="balance_sheet_snapshot")
    _fact(db_session, company, doc, "total_assets", 240.0, period_type="balance_sheet_snapshot")
    missing = _calculate(db_session, company)
    assert _metric(missing, "roe")["reason"] == "missing_average_base_snapshot"
    assert _metric(missing, "roa")["reason"] == "missing_average_base_snapshot"

    _fact(db_session, company, doc, "total_equity", 80.0, period="2021Q3", period_type="balance_sheet_snapshot")
    _fact(db_session, company, doc, "total_assets", 160.0, period="2021Q3", period_type="balance_sheet_snapshot")
    calculated = _calculate(db_session, company)
    assert _metric(calculated, "roe")["value"] == 0.2
    assert _metric(calculated, "roe")["formula"] == "net_income / average_total_equity"
    assert _metric(calculated, "roa")["value"] == 0.1


def test_roe_roa_prefer_previous_year_q4_for_annual_period(db_session):
    company = _company(db_session)
    doc = _doc(db_session, company)
    _fact(db_session, company, doc, "net_income", 20.0, period="2021Q4", period_type="annual")
    _fact(db_session, company, doc, "total_equity", 120.0, period="2021Q4", period_type="balance_sheet_snapshot")
    _fact(db_session, company, doc, "total_assets", 240.0, period="2021Q4", period_type="balance_sheet_snapshot")
    _fact(db_session, company, doc, "total_equity", 80.0, period="2020Q4", period_type="balance_sheet_snapshot")
    _fact(db_session, company, doc, "total_assets", 160.0, period="2020Q4", period_type="balance_sheet_snapshot")
    _fact(db_session, company, doc, "total_equity", 1.0, period="2021Q3", period_type="balance_sheet_snapshot")

    calculated = _calculate(db_session, company)

    assert _metric(calculated, "roe")["value"] == pytest.approx(0.2)
    assert _metric(calculated, "roe")["inputs"]["average_total_equity"]["previous_period"] == "2020Q4"
    assert _metric(calculated, "roa")["value"] == pytest.approx(0.1)


def test_revenue_growth_requires_comparable_period_type(db_session):
    company = _company(db_session)
    doc = _doc(db_session, company)
    _fact(db_session, company, doc, "revenue", 120.0, period="2021Q4", period_type="annual")
    _fact(db_session, company, doc, "revenue", 100.0, period="2021Q3", period_type="annual")
    report = _calculate(db_session, company)
    assert _metric(report, "revenue_growth")["value"] == pytest.approx(0.2)

    company2 = _company(db_session, "MIX")
    doc2 = _doc(db_session, company2)
    _fact(db_session, company2, doc2, "revenue", 120.0, period="2021Q4", period_type="annual")
    _fact(db_session, company2, doc2, "revenue", 100.0, period="2021Q3", period_type="standalone_quarter")
    mixed = _calculate(db_session, company2)
    assert _metric(mixed, "revenue_growth")["status"] == "blocked_by_period_semantics"


def test_missing_zero_denominator_standard_and_fixture_controls(db_session):
    company = _company(db_session)
    doc = _doc(db_session, company)
    _fact(db_session, company, doc, "revenue", 0.0)
    _fact(db_session, company, doc, "net_income", 10.0)
    _fact(db_session, company, doc, "operating_profit", 20.0, standard="RAS")
    _fact(db_session, company, doc, "current_assets", 10.0, period_type="balance_sheet_snapshot", quality_flag="fixture")

    report = _calculate(db_session, company)

    assert _metric(report, "net_margin")["status"] == "blocked_by_zero_denominator"
    assert _metric(report, "operating_margin")["status"] == "missing"
    assert _metric(report, "current_ratio")["status"] == "missing"


def test_debt_to_equity_requires_explicit_total_debt(db_session):
    company = _company(db_session)
    doc = _doc(db_session, company)
    _fact(db_session, company, doc, "short_term_debt", 10.0, period_type="balance_sheet_snapshot")
    _fact(db_session, company, doc, "long_term_debt", 20.0, period_type="balance_sheet_snapshot")
    _fact(db_session, company, doc, "total_equity", 100.0, period_type="balance_sheet_snapshot")

    report = _calculate(db_session, company)

    assert _metric(report, "debt_to_equity")["status"] == "missing"
    assert _metric(report, "debt_to_equity")["inputs_missing"] == ["total_debt"]

    company2 = _company(db_session, "DERD")
    doc2 = _doc(db_session, company2)
    _fact(
        db_session,
        company2,
        doc2,
        "total_debt",
        30.0,
        period_type="balance_sheet_snapshot",
        quality_flag="derived",
        source_location={"formula": "short_term_borrowings + long_term_borrowings"},
    )
    _fact(db_session, company2, doc2, "total_equity", 100.0, period_type="balance_sheet_snapshot")
    derived = _calculate(db_session, company2)
    assert _metric(derived, "debt_to_equity")["reason"] == "explicit_total_debt_missing_no_component_derivation"


def test_banking_sector_policy_marks_industrial_metrics_unsupported(db_session):
    company = Company(
        ticker="BANK1",
        board="TQBR",
        short_name="BANK1",
        full_name="Sberbank",
        aliases_json=[],
        sector="financials",
        subsector="bank",
    )
    db_session.add(company)
    db_session.commit()
    doc = _doc(db_session, company)
    _fact(db_session, company, doc, "net_income", 100.0, period_type="annual")
    _fact(db_session, company, doc, "total_assets", 1_200.0, period_type="balance_sheet_snapshot")
    _fact(db_session, company, doc, "total_equity", 120.0, period_type="balance_sheet_snapshot")

    report = _calculate(db_session, company)

    assert report["sector_policy"]["sector_profile"] == "banking"
    assert _metric(report, "current_ratio")["status"] == "unsupported_by_sector_policy"


def test_dataframe_parse_report_ignores_derived_safe_facts_by_default(tmp_path):
    root = tmp_path
    path = root / "data" / "validation" / "LKOH"
    path.mkdir(parents=True, exist_ok=True)
    (path / "2021Q4_2021Q4_dataframe_statement_fact_parse.json").write_text(
        json.dumps(
            {
                "facts": [
                    {
                        "metric_code": "total_debt",
                        "period": "2021Q4",
                        "value": 30.0,
                        "reporting_standard": "IFRS",
                        "period_type": "balance_sheet_snapshot",
                        "quality_flag": "high_confidence",
                        "extraction_method": "dataframe_statement_parser_derived",
                        "source_location": {"formula": "short + long"},
                    },
                    {
                        "metric_code": "total_equity",
                        "period": "2021Q4",
                        "value": 100.0,
                        "reporting_standard": "IFRS",
                        "period_type": "balance_sheet_snapshot",
                        "quality_flag": "high_confidence",
                        "extraction_method": "dataframe_statement_parser",
                        "source_location": {"raw_label": "Total equity"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    report = FinancialRatiosCalculator(root=root).calculate(
        FinancialRatiosRequest(
            company_ticker="LKOH",
            period_from="2021Q4",
            period_to="2021Q4",
            reporting_standard="IFRS",
            source="dataframe_parse_report",
        )
    ).to_dict()

    assert _metric(report, "debt_to_equity")["status"] == "missing"
    assert _metric(report, "debt_to_equity")["inputs_missing"] == ["total_debt"]


def test_banking_ratios_calculate_only_from_explicit_bank_inputs(db_session):
    company = Company(
        ticker="BANK2",
        board="TQBR",
        short_name="BANK2",
        full_name="Bank Saint Petersburg",
        aliases_json=[],
        sector="financials",
        subsector="bank",
    )
    db_session.add(company)
    db_session.commit()
    doc = _doc(db_session, company)
    for code, value, period_type in [
        ("operating_income", 200.0, "annual"),
        ("operating_expenses", -80.0, "annual"),
        ("net_income", 50.0, "annual"),
        ("loans_to_customers", 700.0, "balance_sheet_snapshot"),
        ("customer_accounts", 1_000.0, "balance_sheet_snapshot"),
        ("total_equity", 150.0, "balance_sheet_snapshot"),
        ("total_assets", 1_500.0, "balance_sheet_snapshot"),
    ]:
        _fact(db_session, company, doc, code, value, period_type=period_type)

    report = _calculate(db_session, company)

    assert _metric(report, "cost_to_income")["value"] == pytest.approx(0.4)
    assert _metric(report, "loan_to_deposit")["value"] == pytest.approx(0.7)
    assert _metric(report, "equity_to_assets")["value"] == pytest.approx(0.1)
    assert _metric(report, "net_margin_like")["value"] == pytest.approx(0.25)
    assert _metric(report, "net_interest_margin")["reason"] == "missing_average_interest_earning_assets"


def test_dataframe_parse_report_excludes_and_warns_for_text_fallback():
    ticker = "DFR"
    root = Path("tests/runtime_financial_ratios") / ticker
    path = root / "data" / "validation" / ticker / "2021Q4_2021Q4_dataframe_statement_fact_parse.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "company_ticker": ticker,
                "period_from": "2021Q4",
                "period_to": "2021Q4",
                "facts": [
                    _candidate("revenue", 100.0),
                    _candidate("net_income", 10.0),
                ],
            }
        ),
        encoding="utf-8",
    )
    calculator = FinancialRatiosCalculator(root=root)
    excluded = calculator.calculate(
        FinancialRatiosRequest(
            company_ticker=ticker,
            period_from="2021Q4",
            period_to="2021Q4",
            source="dataframe_parse_report",
        )
    ).to_dict()
    assert _metric(excluded, "net_margin")["status"] == "missing"

    allowed = calculator.calculate(
        FinancialRatiosRequest(
            company_ticker=ticker,
            period_from="2021Q4",
            period_to="2021Q4",
            source="dataframe_parse_report",
            allow_text_fallback_candidates=True,
        )
    ).to_dict()
    net_margin = _metric(allowed, "net_margin")
    assert net_margin["value"] == 0.1
    assert net_margin["trust_warning"] == "metric_uses_text_fallback_fact_candidates"


def _candidate(metric_code: str, value: float) -> dict:
    return {
        "metric_code": metric_code,
        "period": "2021Q4",
        "reporting_standard": "IFRS",
        "value": value,
        "period_type": "annual",
        "quality_flag": "high_confidence_text_fallback",
        "source_document_id": 1,
        "source_location": {"fact_source_kind": "text_table_fallback_semantic_gate"},
        "extraction_method": "text_table_fallback_semantic_gate",
        "raw_label": metric_code,
    }


def teardown_module():
    shutil.rmtree("tests/runtime_financial_ratios", ignore_errors=True)
