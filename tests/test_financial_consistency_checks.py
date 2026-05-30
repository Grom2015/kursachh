from app.db.models import MetricValue, StatementFact
from app.services.quality.financial_consistency_checks import run_financial_consistency_checks


def _fact(metric, value):
    return StatementFact(
        company_id=1,
        period="2021Q1",
        reporting_standard="IFRS",
        statement_type="financial_position",
        metric_code=metric,
        value=value,
        unit_multiplier=1.0,
        period_type="balance_sheet_snapshot",
        source_location={"source_role": "financial_statements"},
        quality_flag="exact",
        confidence_score=0.9,
    )


def test_current_assets_cannot_exceed_total_assets():
    result = run_financial_consistency_checks([_fact("current_assets", 120), _fact("total_assets", 100)])

    assert any(item["check"] == "current_assets_lte_total_assets" for item in result.failed_checks)


def test_total_debt_and_cash_non_negative_checks_pass():
    result = run_financial_consistency_checks([_fact("total_debt", 10), _fact("cash_and_equivalents", 5)])

    assert "2021Q1: total_debt >= 0" in result.passed_checks
    assert "2021Q1: cash_and_equivalents >= 0" in result.passed_checks


def test_fcf_recomputes_from_operating_cash_flow_minus_capex():
    metric = MetricValue(
        company_id=1,
        period="2021Q1",
        metric_code="fcf",
        metric_name="FCF",
        value=80,
        display_value="80",
        formula="operating_cash_flow - capex",
        inputs_json={"operating_cash_flow": 100, "capex": 20},
        quality_flag="exact",
        warnings_json=[],
    )

    result = run_financial_consistency_checks([], [metric])

    assert "2021Q1: fcf recomputes correctly" in result.passed_checks


def test_same_metric_period_conflict_fails_beyond_tolerance():
    result = run_financial_consistency_checks([_fact("revenue", 100), _fact("revenue", 130)])

    assert any(item["check"] == "same_metric_period_conflict" for item in result.failed_checks)
