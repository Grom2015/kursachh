from app.tools.validate_real_extraction import empty_report


def test_gazp_live_download_blocked_returns_controlled_fail():
    report = empty_report(
        "GAZP",
        "2021Q1",
        "2021Q4",
        "live",
        "No real documents found or downloaded.",
        ["Report download failed for https://www.gazprom.com/report.pdf: network unavailable"],
    )

    assert report["status"] == "FAIL"
    assert report["fixture_data_used"] is False
    assert any("network unavailable" in warning for warning in report["warnings"])


def test_gazp_replay_cache_missing_returns_controlled_fail():
    report = empty_report(
        "GAZP",
        "2021Q1",
        "2021Q4",
        "replay_cache",
        "Cached real documents not found; run live validation first.",
    )

    assert report["status"] == "FAIL"
    assert report["validation_mode"] == "replay_cache"
    assert report["summary"]["source_document_count"] == 0
    assert "Cached real documents not found" in " ".join(report["warnings"])


def test_gazp_parser_diagnostics_report_exists_when_facts_zero():
    report = empty_report("GAZP", "2021Q1", "2021Q4", "replay_cache", "no docs")

    assert report["parser_diagnostics"] == {
        "documents_with_tables": 0,
        "documents_without_tables": 0,
        "candidate_rows_count": 0,
        "strong_matches_count": 0,
        "weak_matches_count": 0,
    }


def test_gazp_real_mode_never_reports_fixture_fallback():
    report = empty_report("GAZP", "2021Q1", "2021Q4", "live", "no docs")

    assert report["data_mode"] == "real"
    assert report["fixture_data_used"] is False
