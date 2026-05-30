from app.db.models import StatementFact
from app.services.metrics.metric_engine import MetricEngine


def add_fact(db, company_id, period, code, value):
    db.add(
        StatementFact(
            company_id=company_id,
            period=period,
            reporting_standard="IFRS",
            statement_type="other",
            metric_code=code,
            value=value,
            currency="RUB",
            unit_multiplier=1,
            period_type="quarter",
            quality_flag="exact",
        )
    )


def add_core_period(db, company_id, period, revenue=100, net_income=20, equity=100, assets=200):
    values = {
        "net_income": net_income,
        "total_equity": equity,
        "current_assets": 50,
        "current_liabilities": 25,
        "operating_cash_flow": 30,
        "capex": 10,
        "revenue": revenue,
        "ebitda": 25,
        "operating_profit": 22,
        "total_assets": assets,
        "total_debt": 40,
        "cash_and_equivalents": 5,
    }
    for code, value in values.items():
        add_fact(db, company_id, period, code, value)


def test_formula_engine_core_metrics(db_session):
    company_id = 1
    for period in ["2021Q1", "2021Q2", "2021Q3", "2021Q4"]:
        add_core_period(db_session, company_id, period)
    db_session.commit()
    metrics = MetricEngine(db_session).calculate(company_id, ["2021Q1", "2021Q2", "2021Q3", "2021Q4"])
    by_code = {metric.metric_code: metric for metric in metrics}
    q4 = {(metric.metric_code, metric.period): metric for metric in metrics}
    assert q4[("roe", "2021Q4")].value == 0.8
    assert by_code["current_ratio"].value == 2
    assert by_code["fcf"].value == 20


def test_missing_input_returns_missing(db_session):
    metrics = MetricEngine(db_session).calculate(1, ["2021Q2"])
    by_code = {metric.metric_code: metric for metric in metrics}
    assert by_code["current_ratio"].quality_flag == "missing"


def test_division_by_zero_does_not_crash(db_session):
    add_fact(db_session, 1, "2021Q2", "current_assets", 50)
    add_fact(db_session, 1, "2021Q2", "current_liabilities", 0)
    db_session.commit()
    metrics = MetricEngine(db_session).calculate(1, ["2021Q2"])
    current_ratio = {metric.metric_code: metric for metric in metrics}["current_ratio"]
    assert current_ratio.value is None
    assert current_ratio.quality_flag == "missing"
    assert "denominator_zero" in current_ratio.warnings_json


def test_missing_denominator_returns_missing(db_session):
    add_fact(db_session, 1, "2021Q2", "current_assets", 50)
    db_session.commit()
    metrics = MetricEngine(db_session).calculate(1, ["2021Q2"])
    current_ratio = {metric.metric_code: metric for metric in metrics}["current_ratio"]
    assert current_ratio.value is None
    assert current_ratio.quality_flag == "missing"
    assert "missing input" in current_ratio.warnings_json


def test_negative_equity_is_calculated_with_warning(db_session):
    for period in ["2021Q1", "2021Q2", "2021Q3", "2021Q4"]:
        add_core_period(db_session, 1, period, equity=-100)
    db_session.commit()
    metrics = MetricEngine(db_session).calculate(1, ["2021Q1", "2021Q2", "2021Q3", "2021Q4"])
    debt_to_equity = {
        (metric.metric_code, metric.period): metric for metric in metrics
    }[("debt_to_equity", "2021Q4")]
    assert debt_to_equity.value == -0.4
    assert any("Negative denominator" in warning for warning in debt_to_equity.warnings_json)


def test_negative_fcf_is_allowed(db_session):
    add_fact(db_session, 1, "2021Q2", "operating_cash_flow", 10)
    add_fact(db_session, 1, "2021Q2", "capex", 30)
    add_fact(db_session, 1, "2021Q2", "revenue", 100)
    db_session.commit()
    metrics = MetricEngine(db_session).calculate(1, ["2021Q2"])
    by_code = {metric.metric_code: metric for metric in metrics}
    assert by_code["fcf"].value == -20
    assert by_code["fcf_margin"].value == -0.2


def test_incomplete_ttm_is_missing(db_session):
    for period in ["2021Q2", "2021Q3", "2021Q4"]:
        add_core_period(db_session, 1, period)
    db_session.commit()
    metrics = MetricEngine(db_session).calculate(1, ["2021Q2", "2021Q3", "2021Q4"])
    roe = {(metric.metric_code, metric.period): metric for metric in metrics}[("roe", "2021Q4")]
    assert roe.quality_flag == "missing"
    assert any("TTM unavailable" in warning for warning in roe.warnings_json)


def test_period_without_previous_balance_sheet_uses_fallback(db_session):
    for period in ["2021Q1", "2021Q2", "2021Q3", "2021Q4"]:
        add_fact(db_session, 1, period, "net_income", 20)
    add_fact(db_session, 1, "2021Q4", "total_equity", 100)
    db_session.commit()
    metrics = MetricEngine(db_session).calculate(1, ["2021Q1", "2021Q2", "2021Q3", "2021Q4"])
    roe = {(metric.metric_code, metric.period): metric for metric in metrics}[("roe", "2021Q4")]
    assert roe.quality_flag == "derived"
    assert any("current equity used" in warning for warning in roe.warnings_json)
