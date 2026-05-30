from app.tools.compare_real_peers import (
    analytical_readiness,
    compatibility_decision,
    ebitda_metrics_available,
    is_full_year_available,
    valuation_metrics_available,
)


def _metric(
    code: str = "net_margin",
    status: str = "valid",
    period_type: str = "ytd",
    months: int = 6,
    quality: str = "exact",
):
    return {
        "metric_code": code,
        "period": "2021Q2",
        "status": status,
        "value": 0.1,
        "quality_flag": quality,
        "input_period_types": {"net_income": period_type, "revenue": period_type},
        "input_source_roles": {"net_income": "financial_statements", "revenue": "financial_statements"},
        "input_quality_flags": {"net_income": quality, "revenue": quality},
        "input_source_locations": [
            {
                "source_url": "https://issuer.example/report.pdf",
                "raw_label": "Profit for the period",
                "source_role": "financial_statements",
                "period_type": period_type,
                "ytd_months": months if period_type == "ytd" else None,
            }
        ],
    }


def test_comparable_only_when_both_metrics_valid():
    decision = compatibility_decision(_metric(), _metric(), "net_margin")

    assert decision["comparison_status"] == "comparable"
    assert decision["coverage"] == "6M"


def test_questionable_metric_excluded():
    decision = compatibility_decision(_metric(status="questionable"), _metric(), "revenue_growth")

    assert decision["comparison_status"] == "excluded"
    assert decision["reason"] == "questionable_for_target"


def test_incompatible_period_type_excluded():
    decision = compatibility_decision(_metric(period_type="quarter"), _metric(period_type="ytd"), "net_margin")

    assert decision["reason"] == "incompatible_period_coverage"


def test_annual_vs_9m_excluded():
    decision = compatibility_decision(_metric(period_type="annual"), _metric(period_type="ytd", months=9), "net_margin")

    assert decision["reason"] == "incompatible_period_coverage"


def test_valuation_metrics_excluded_with_reason():
    decision = compatibility_decision(_metric("pe_ratio"), _metric("pe_ratio"), "pe_ratio")

    assert decision["reason"] == "valuation_inputs_missing"
    assert valuation_metrics_available([]) is False


def test_ebitda_metrics_excluded_with_reason():
    decision = compatibility_decision(_metric("ebitda_margin"), _metric("ebitda_margin"), "ebitda_margin")

    assert decision["reason"] == "ebitda_missing"
    assert ebitda_metrics_available([]) is False


def test_q4_missing_disables_full_year_comparison():
    assert is_full_year_available({"TATN": {"missing_periods": ["2021Q4"]}}, "TATN") is False


def test_analytical_readiness_limited_or_not_ready_never_ready():
    assert analytical_readiness(4, full_year_available=False) == "not_ready"
    assert analytical_readiness(5, full_year_available=False) == "limited"
    assert analytical_readiness(5, full_year_available=True) == "usable_with_warnings"
