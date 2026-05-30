from app.db.models import StatementFact
from app.services.metrics.metric_engine import MetricEngine
from app.services.metrics.metric_methodology_registry import METHODOLOGIES
from app.tools.audit_statement_based_metrics import audit_row, valuation_metrics_blocked


def _fact(company_id: int, period: str, code: str, value: float, period_type: str = "ytd", quality: str = "exact"):
    return StatementFact(
        company_id=company_id,
        period=period,
        reporting_standard="IFRS",
        statement_type="ifrs",
        metric_code=code,
        value=value,
        currency="RUB",
        unit_multiplier=1,
        period_type=period_type,
        quality_flag=quality,
        source_location={
            "source_url": "https://issuer.example/report.pdf",
            "raw_label": code.replace("_", " ").title(),
            "source_role": "financial_statements",
            "statement_context": "profit_or_loss" if code in {"revenue", "net_income"} else "financial_position",
            "page": 1,
            "table": "statement_text",
            "line": 10,
        },
    )


def _add_fact(db, company_id: int, period: str, code: str, value: float, period_type: str = "ytd", quality: str = "exact"):
    db.add(_fact(company_id, period, code, value, period_type, quality))


def test_methodology_registry_covers_all_metrics():
    expected = {
        "revenue_growth",
        "ebitda_margin",
        "operating_margin",
        "net_margin",
        "roe",
        "roa",
        "debt_to_equity",
        "net_debt_to_ebitda",
        "current_ratio",
        "fcf",
        "fcf_margin",
        "pe_ratio",
        "ev_to_ebitda",
        "dividend_yield",
        "net_interest_margin",
        "cost_to_income",
        "loan_to_deposit",
        "equity_to_assets",
        "net_margin_like",
    }

    assert set(METHODOLOGIES) == expected


def test_metric_calculated_only_when_required_canonical_facts_exist(db_session):
    _add_fact(db_session, 1, "2021Q2", "net_income", 20)
    _add_fact(db_session, 1, "2021Q2", "revenue", 100)
    db_session.commit()

    metrics = MetricEngine(db_session).calculate(1, ["2021Q2"], exclude_fixture=True)
    net_margin = {metric.metric_code: metric for metric in metrics}["net_margin"]

    assert net_margin.value == 0.2
    assert net_margin.inputs_json["source_references"]["net_income"]["source_document_id"] is None
    assert net_margin.inputs_json["source_references"]["net_income"]["source_location"] == "page 1, table statement_text, line 10"


def test_missing_input_returns_missing_not_fake_value(db_session):
    _add_fact(db_session, 1, "2021Q2", "revenue", 100)
    db_session.commit()

    metrics = MetricEngine(db_session).calculate(1, ["2021Q2"], exclude_fixture=True)
    net_margin = {metric.metric_code: metric for metric in metrics}["net_margin"]

    assert net_margin.value is None
    assert net_margin.quality_flag == "missing"
    assert "missing input" in net_margin.warnings_json


def test_denominator_zero_returns_controlled_warning(db_session):
    _add_fact(db_session, 1, "2021Q2", "net_income", 20)
    _add_fact(db_session, 1, "2021Q2", "revenue", 0)
    db_session.commit()

    metrics = MetricEngine(db_session).calculate(1, ["2021Q2"], exclude_fixture=True)
    net_margin = {metric.metric_code: metric for metric in metrics}["net_margin"]

    assert net_margin.value is None
    assert net_margin.quality_flag == "missing"
    assert "denominator_zero" in net_margin.warnings_json


def test_fcf_uses_operating_cash_flow_minus_capex(db_session):
    _add_fact(db_session, 1, "2021Q2", "operating_cash_flow", 100)
    _add_fact(db_session, 1, "2021Q2", "capex", 30)
    db_session.commit()

    metrics = MetricEngine(db_session).calculate(1, ["2021Q2"], exclude_fixture=True)
    fcf = {metric.metric_code: metric for metric in metrics}["fcf"]

    assert fcf.value == 70


def test_capex_from_segment_note_preserves_methodology_warning():
    row = audit_row(
        "GAZP",
        "2021Q4",
        "fcf",
        {
            "metric_code": "fcf",
            "period": "2021Q4",
            "status": "valid",
            "methodology_warnings": [
                "capex uses reported segment capital expenditures, not cash-flow purchase of PPE."
            ],
            "input_period_types": {"operating_cash_flow": "annual", "capex": "annual"},
            "input_source_locations": [
                {"source_url": "https://issuer.example/report.pdf", "raw_label": "Capital expenditures1"}
            ],
        },
        METHODOLOGIES["fcf"],
        {"review_pack_status": "PASS"},
        {"status": "verified_review_pack"},
    )

    assert any("reported segment capital expenditures" in warning for warning in row["warnings"])


def test_roe_fallback_to_ending_equity_is_questionable():
    row = audit_row(
        "LKOH",
        "2021Q4",
        "roe",
        {
            "metric_code": "roe",
            "period": "2021Q4",
            "status": "questionable",
            "methodology_warnings": ["Average equity unavailable; current equity used"],
            "input_period_types": {"net_income": "annual", "total_equity": "balance_sheet_snapshot"},
        },
        METHODOLOGIES["roe"],
        {"review_pack_status": "PASS"},
        {"status": "verified_review_pack"},
    )

    assert row["status"] == "questionable"
    assert "Average equity unavailable; current equity used" in row["warnings"]


def test_pe_missing_without_market_cap_and_ev_ebitda_missing_without_inputs():
    rows = [
        audit_row(
            "LKOH",
            "2021Q4",
            "pe_ratio",
            {"metric_code": "pe_ratio", "period": "2021Q4", "status": "missing", "blocking_inputs": ["market_cap"]},
            METHODOLOGIES["pe_ratio"],
            {},
            {"status": "unverified"},
        ),
        audit_row(
            "LKOH",
            "2021Q4",
            "ev_to_ebitda",
            {
                "metric_code": "ev_to_ebitda",
                "period": "2021Q4",
                "status": "missing",
                "blocking_inputs": ["enterprise_value", "ebitda"],
            },
            METHODOLOGIES["ev_to_ebitda"],
            {},
            {"status": "unverified"},
        ),
    ]

    assert valuation_metrics_blocked(rows) is True
    assert all(row["market_inputs_required"] for row in rows)


def test_ytd_metrics_are_not_treated_as_standalone_quarter(db_session):
    _add_fact(db_session, 1, "2021Q2", "revenue", 100, "ytd")
    _add_fact(db_session, 1, "2021Q2", "operating_profit", 10, "quarter")
    db_session.commit()

    metrics = MetricEngine(db_session).calculate(1, ["2021Q2"], exclude_fixture=True)
    operating_margin = {metric.metric_code: metric for metric in metrics}["operating_margin"]

    assert operating_margin.value is not None
    assert any("Mixed period coverage" in warning for warning in operating_margin.warnings_json)
    assert operating_margin.quality_flag == "derived"


def test_no_fixture_data_used_in_real_mode(db_session):
    _add_fact(db_session, 1, "2021Q2", "net_income", 20, quality="fixture")
    _add_fact(db_session, 1, "2021Q2", "revenue", 100, quality="fixture")
    db_session.commit()

    metrics = MetricEngine(db_session).calculate(1, ["2021Q2"], exclude_fixture=True)
    net_margin = {metric.metric_code: metric for metric in metrics}["net_margin"]

    assert net_margin.value is None
    assert net_margin.quality_flag == "missing"
