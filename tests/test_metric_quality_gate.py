from app.db.models import MetricValue
from app.services.quality.metric_quality_gate import evaluate_metric_quality


def _metric(code, value=1.0, quality="exact", warnings=None, inputs=None):
    return MetricValue(
        company_id=1,
        period="2021Q1",
        metric_code=code,
        metric_name=code,
        value=value,
        display_value=str(value) if value is not None else None,
        formula="test",
        inputs_json=inputs or {},
        quality_flag=quality,
        warnings_json=warnings or [],
    )


def test_valuation_metrics_remain_missing_without_market_inputs():
    result = evaluate_metric_quality([_metric("pe_ratio", value=None, quality="missing")])

    assert result.status == "missing"
    assert result.missing_count == 1


def test_valuation_metric_with_value_blocks_quality_gate():
    result = evaluate_metric_quality([_metric("pe_ratio", value=5.0)])

    assert result.status == "fail"
    assert "Valuation metric calculated without validated market inputs." in result.blockers


def test_no_fixture_metrics_in_real_mode():
    result = evaluate_metric_quality([_metric("net_margin", quality="fixture")], data_mode="real")

    assert result.status == "fail"
    assert "Fixture metrics are present in real mode." in result.blockers


def test_denominator_zero_with_value_blocks_quality_gate():
    result = evaluate_metric_quality([_metric("net_margin", value=0.1, warnings=["denominator_zero"])])

    assert result.status == "fail"
    assert "Metric with denominator_zero warning has a non-null value." in result.blockers


def test_capex_methodology_warning_preserved():
    result = evaluate_metric_quality(
        [
            _metric(
                "fcf",
                value=10.0,
                warnings=["capex uses reported segment capital expenditures, not cash-flow purchase of PPE."],
            )
        ]
    )

    assert "Capex methodology warning preserved for capex-based metric." in result.warnings
