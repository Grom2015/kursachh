from app.services.quality.metric_quality_gate import evaluate_metric_quality
from app.services.quality.readiness_scope import evaluate_readiness_scopes


def _metric(code, status="missing", value=None, quality="missing", warnings=None):
    return {
        "metric_code": code,
        "period": "2021Q1",
        "status": status,
        "value": value,
        "quality_flag": quality,
        "warnings": warnings or [],
        "inputs": {},
    }


def test_valuation_metrics_do_not_reduce_statement_based_score():
    result = evaluate_metric_quality(
        [
            _metric("net_margin", "valid", 0.1, "exact"),
            _metric("pe_ratio"),
            _metric("ev_to_ebitda"),
            _metric("dividend_yield"),
        ]
    )

    assert result.score == 1.0
    assert result.unsupported_metrics_count == 3
    assert all(row["reason"] == "market_or_valuation_inputs_required" for row in result.unsupported_by_policy)


def test_ebitda_metrics_without_disclosure_do_not_reduce_statement_score():
    result = evaluate_metric_quality(
        [
            _metric("operating_margin", "valid", 0.2, "exact"),
            _metric("ebitda_margin"),
            _metric("net_debt_to_ebitda"),
        ]
    )

    assert result.score == 1.0
    assert {row["metric_code"] for row in result.unsupported_by_policy} == {"ebitda_margin", "net_debt_to_ebitda"}


def test_missing_current_ratio_inputs_reduce_statement_readiness():
    result = evaluate_metric_quality(
        [
            _metric("net_margin", "valid", 0.1, "exact"),
            _metric("current_ratio"),
        ]
    )

    assert result.score == 0.5
    assert result.missing_expected_metrics_count == 1
    assert result.missing_but_expected[0]["metric_code"] == "current_ratio"


def test_methodology_sensitive_metrics_have_separate_breakdown():
    result = evaluate_metric_quality(
        [
            _metric("net_margin", "valid", 0.1, "exact"),
            _metric("roe", "questionable", 0.05, "derived", ["Average equity unavailable"]),
            _metric("revenue_growth", "questionable", 0.2, "derived", ["incomplete_period_coverage"]),
        ]
    )

    assert result.score == 1.0
    assert {row["metric_code"] for row in result.methodology_sensitive} == {"roe", "revenue_growth"}


def test_scope_auto_ready_is_scoped_not_full_company_ready():
    metric_result = evaluate_metric_quality(
        [
            _metric("operating_margin", "valid", 0.2, "exact"),
            _metric("net_margin", "valid", 0.1, "exact"),
            _metric("current_ratio", "valid", 1.5, "exact"),
            _metric("pe_ratio"),
        ]
    )
    scopes = evaluate_readiness_scopes("pass", "pass", metric_result, {"failed_checks": []})
    payload = scopes.to_dict()

    assert payload["scope_statuses"]["statement_based_financials"]["status"] == "AUTO_READY"
    assert payload["scope_statuses"]["valuation_metrics"]["status"] == "UNAVAILABLE"
    assert "valuation_metrics" in payload["unsupported_output_scopes"]


def test_manual_fail_blocks_scoped_readiness():
    metric_result = evaluate_metric_quality([_metric("net_margin", "valid", 0.1, "exact")])

    scopes = evaluate_readiness_scopes(
        "pass",
        "pass",
        metric_result,
        {"failed_checks": []},
        manual_verification={"status": "fail"},
    )

    assert scopes.scope_statuses["statement_based_financials"]["status"] != "AUTO_READY"
    assert "manual_qa_failed" in scopes.scope_statuses["statement_based_financials"]["critical_blockers"]
