from app.db.models import AnalysisJob, AnalysisResult
from app.services.analysis.orchestrator import AnalysisOrchestrator


def _job(db, data_mode):
    job = AnalysisJob(
        company_query="LKOH",
        period_from="2021Q2",
        period_to="2021Q4",
        reporting_standard="IFRS",
        include_market_data=True,
        include_peers=False,
        data_mode=data_mode,
    )
    db.add(job)
    db.commit()
    AnalysisOrchestrator(db).run(job.id)
    db.refresh(job)
    return job


def test_fixture_mode_uses_fixture_only(db_session):
    job = _job(db_session, "fixture")
    assert job.status == "succeeded"
    result = job.result_id and db_session.get(AnalysisResult, job.result_id)
    assert result.data_snapshot_json["data_mode"] == "fixture"
    assert result.data_snapshot_json["fixture_data_used"] is True
    assert result.data_snapshot_json["real_data_used"] is False


def test_real_mode_does_not_fallback_silently(db_session):
    job = _job(db_session, "real")
    assert job.status == "failed"
    assert "Real report documents unavailable" in job.error_message
    assert job.result_id is None


def test_auto_mode_fallback_adds_warning(db_session):
    job = _job(db_session, "auto")
    assert job.status == "succeeded"
    assert "Real data unavailable; fixture fallback used." in job.warnings_json
