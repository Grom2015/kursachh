from app.tools.audit_metric_coverage_gaps import audit_gaps, priority_score


def _metric(
    metric_code: str,
    period: str,
    status: str,
    blocking_inputs: list[str] | None = None,
    quality_flag: str = "exact",
) -> dict:
    return {
        "metric_code": metric_code,
        "period": period,
        "status": status,
        "quality_flag": quality_flag,
        "blocking_inputs": blocking_inputs or [],
        "inputs": {},
        "input_quality_flags": {},
        "methodology_warnings": [],
    }


def _report(valid: int = 0, questionable: int = 0, missing: int = 0, metrics: list[dict] | None = None) -> dict:
    return {
        "summary": {
            "metrics_valid_count": valid,
            "metrics_questionable_count": questionable,
            "metrics_missing_count": missing,
        },
        "metrics": metrics or [],
    }


def _golden(pass_status: bool = True) -> dict:
    return {"review_pack_status": "PASS" if pass_status else "NO_VERIFIED_CHECKS"}


def test_missing_current_assets_liabilities_rank_high_for_current_ratio():
    reports = {
        "LKOH": _report(
            valid=1,
            metrics=[_metric("current_ratio", "2021Q1", "valid")],
        ),
        "TATN": _report(
            missing=1,
            metrics=[
                _metric(
                    "current_ratio",
                    "2021Q1",
                    "missing",
                    ["current_assets", "current_liabilities"],
                    quality_flag="missing",
                )
            ],
        ),
        "GAZP": _report(missing=1, metrics=[]),
    }

    report = audit_gaps(["LKOH", "TATN", "GAZP"], "2021Q1", "2021Q1", reports, {})

    top = report["missing_fact_priority"][:2]
    assert {item["fact_code"] for item in top} == {"current_assets", "current_liabilities"}
    assert all("current_ratio" in item["unblocks_metrics"] for item in top)
    current_ratio = next(row for row in report["missing_metric_matrix"] if row["metric_code"] == "current_ratio")
    assert current_ratio["peer_comparison_impact"] == "high"


def test_ebitda_metrics_are_blocked_by_explicit_disclosure_policy():
    reports = {
        "LKOH": _report(metrics=[_metric("ebitda_margin", "2021Q1", "missing", ["ebitda"], "missing")]),
        "TATN": _report(metrics=[_metric("ebitda_margin", "2021Q1", "missing", ["ebitda"], "missing")]),
    }

    report = audit_gaps(["LKOH", "TATN"], "2021Q1", "2021Q1", reports, {})

    assert report["ebitda_blockers"]["policy"].startswith("Do not derive EBITDA")
    assert "ebitda" not in {item["fact_code"] for item in report["missing_fact_priority"]}


def test_valuation_metrics_are_separated_as_market_module_blockers():
    reports = {
        "LKOH": _report(metrics=[_metric("pe_ratio", "2021Q1", "missing", ["market_cap"], "missing")]),
        "GAZP": _report(metrics=[_metric("pe_ratio", "2021Q1", "missing", ["market_cap"], "missing")]),
    }

    report = audit_gaps(["LKOH", "GAZP"], "2021Q1", "2021Q1", reports, {})

    assert "pe_ratio" in report["valuation_blockers"]
    assert not any(item["fact_code"] == "market_cap" for item in report["missing_fact_priority"])


def test_priority_score_increases_with_periods_and_peer_impact():
    low = priority_score("current_assets", ["2021Q1"], ["current_ratio"], [])
    high = priority_score(
        "current_assets",
        ["2021Q1", "2021Q2", "2021Q3", "2021Q4"],
        ["current_ratio"],
        ["LKOH_vs_GAZP", "GAZP_vs_TATN"],
    )

    assert high > low


def test_report_includes_all_companies_and_verified_review_pack_flags():
    reports = {
        "LKOH": _report(valid=24),
        "TATN": _report(valid=4, missing=51),
        "GAZP": _report(valid=2, missing=51),
    }
    golden = {"LKOH": _golden(), "TATN": _golden(), "GAZP": _golden()}

    report = audit_gaps(["LKOH", "TATN", "GAZP"], "2021Q1", "2021Q1", reports, golden)

    assert report["companies"] == ["LKOH", "TATN", "GAZP"]
    assert report["metric_coverage_by_company"]["LKOH"]["verified_review_pack"] is True
    assert report["metric_coverage_by_company"]["GAZP"]["valid"] == 2


def test_fixture_metrics_are_ignored():
    reports = {
        "LKOH": _report(metrics=[_metric("current_ratio", "2021Q1", "valid", quality_flag="fixture")]),
        "TATN": _report(metrics=[]),
    }

    report = audit_gaps(["LKOH", "TATN"], "2021Q1", "2021Q1", reports, {})

    row = next(item for item in report["missing_metric_matrix"] if item["metric_code"] == "current_ratio")
    assert row["LKOH"] == "missing"
    assert "fixture metrics ignored" in report["warnings"][0]


def test_audit_does_not_invoke_parser():
    reports = {
        "LKOH": _report(metrics=[_metric("current_ratio", "2021Q1", "valid")]),
        "GAZP": _report(metrics=[_metric("current_ratio", "2021Q1", "missing", ["current_assets"], "missing")]),
    }

    report = audit_gaps(["LKOH", "GAZP"], "2021Q1", "2021Q1", reports, {})

    assert report["metric_unlock_plan"]
    assert all("parser" not in warning.casefold() for warning in report["warnings"])
