from app.tools.scan_peer_source_availability import (
    candidate_report,
    choose_recommended_candidate,
    readiness_for,
    source_package_score,
)


def coverage(*roles):
    return {
        f"2021Q{index}": {"source_role": role, "trusted_domain": True, "expected_file_type": "pdf"}
        for index, role in enumerate(roles, start=1)
    }


def test_candidate_with_fy_and_interim_is_ready():
    roles = {"2021Q1": "financial_statements", "2021Q4": "annual_report"}

    assert readiness_for(roles, fy_source_verified=True) == "READY_CANDIDATE"


def test_candidate_with_interim_only_is_partial():
    roles = {"2021Q1": "financial_statements", "2021Q2": "financial_statements", "2021Q4": "missing"}

    assert readiness_for(roles, fy_source_verified=False) == "PARTIAL_CANDIDATE"


def test_candidate_with_only_press_releases_not_suitable():
    roles = {"2021Q1": "press_release", "2021Q2": "press_release", "2021Q4": "missing"}

    assert readiness_for(roles, fy_source_verified=False) == "NOT_SUITABLE"


def test_untrusted_domain_penalized():
    manifest = {
        "coverage": {
            "2021Q1": {"source_role": "financial_statements", "trusted_domain": False, "expected_file_type": "pdf"},
            "2021Q2": {"source_role": "financial_statements", "trusted_domain": True, "expected_file_type": "pdf"},
            "2021Q3": {"source_role": "financial_statements", "trusted_domain": True, "expected_file_type": "pdf"},
            "2021Q4": {"source_role": "annual_report", "trusted_domain": True, "expected_file_type": "pdf"},
        }
    }

    assert source_package_score(manifest, ["2021Q1", "2021Q2", "2021Q3", "2021Q4"]) == 2


def test_tatn_with_missing_fy_remains_partial_candidate():
    report = candidate_report("TATN", ["2021Q1", "2021Q2", "2021Q3", "2021Q4"])

    assert report["readiness"] == "PARTIAL_CANDIDATE"
    assert report["fy_source_verified"] is False


def test_recommended_candidate_chosen_by_score_and_readiness():
    candidates = [
        {"ticker": "TATN", "readiness": "PARTIAL_CANDIDATE", "source_package_score": 3, "trusted_sources_count": 3},
        {"ticker": "GAZP", "readiness": "READY_CANDIDATE", "source_package_score": 5, "trusted_sources_count": 4},
    ]

    assert choose_recommended_candidate(candidates)["ticker"] == "GAZP"


def test_no_recommended_candidate_when_none_ready():
    candidates = [{"ticker": "TATN", "readiness": "PARTIAL_CANDIDATE", "source_package_score": 3, "trusted_sources_count": 3}]

    assert choose_recommended_candidate(candidates) is None


def test_scan_logic_has_no_fixture_or_parser_dependency():
    report = candidate_report("GAZP", ["2021Q1", "2021Q2", "2021Q3", "2021Q4"])

    assert "fixture" not in str(report).casefold()
    assert "parser" not in str(report).casefold()
