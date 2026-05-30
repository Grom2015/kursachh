from app.tools import analyze_auto_ready_blockers as analysis


def _validation_report(scope_status="AUTO_PARTIAL", metric_score=0.5):
    return {
        "summary": {
            "canonical_facts_count": 10,
            "metrics_valid_count": 2,
            "metrics_questionable_count": 1,
            "metrics_missing_count": 3,
        },
        "automated_support_status": {
            "status": "AUTO_PARTIAL",
            "source_package_status": "READY",
            "metric_quality_score": 0.2,
            "automated_quality_score": 0.6,
            "scope_statuses": {
                "statement_based_financials": {
                    "status": scope_status,
                    "score": metric_score,
                    "unsupported_by_policy": [
                        {
                            "metric_code": "pe_ratio",
                            "period": "2021Q1",
                            "support_class": "market_valuation_required",
                            "reason": "market_or_valuation_inputs_required",
                        }
                    ],
                    "missing_but_expected": [
                        {
                            "metric_code": "current_ratio",
                            "period": "2021Q1",
                            "required_facts": ["current_assets", "current_liabilities"],
                        }
                    ],
                    "methodology_sensitive": [
                        {
                            "metric_code": "roe",
                            "period": "2021Q1",
                            "status": "questionable",
                        }
                    ],
                }
            },
        },
    }


def test_blocker_analysis_reports_old_new_scores_and_categories(monkeypatch):
    monkeypatch.setattr(analysis, "load_validation_report", lambda *_args: _validation_report())
    monkeypatch.setattr(analysis, "save_report", lambda report, period_from: None)

    report = analysis.analyze(["LKOH"], "2021Q1", "2021Q4")
    row = report["company_results"][0]

    assert row["old_metric_quality_score"] == 0.3333
    assert row["new_statement_based_financials_score"] == 0.5
    assert row["unsupported_by_policy_count"] == 1
    assert row["missing_but_expected_facts"][0]["fact_code"] == "current_assets"


def test_best_next_action_prioritizes_missing_balance_sheet_inputs(monkeypatch):
    monkeypatch.setattr(analysis, "load_validation_report", lambda *_args: _validation_report(metric_score=0.1))
    monkeypatch.setattr(analysis, "save_report", lambda report, period_from: None)

    report = analysis.analyze(["GAZP"], "2021Q1", "2021Q4")

    assert report["best_next_engineering_action"]["target_company"] == "GAZP"
    assert "current_assets/current_liabilities" in report["best_next_engineering_action"]["recommendation"]
