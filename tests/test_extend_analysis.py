from copy import deepcopy

from app.db.models import AnalysisJob, AnalysisResult
from app.services.analysis.extend_service import ExtendAnalysisService
from app.services.analysis.orchestrator import AnalysisOrchestrator


def create_base_result(db_session):
    job = AnalysisJob(
        company_query="LKOH",
        period_from="2021Q2",
        period_to="2021Q4",
        reporting_standard="IFRS",
        include_market_data=True,
        include_peers=True,
        include_news=False,
    )
    db_session.add(job)
    db_session.commit()
    AnalysisOrchestrator(db_session).run(job.id)
    db_session.refresh(job)
    return job.result_id


def test_extend_creates_new_version_with_delta(db_session):
    result_id = create_base_result(db_session)
    old_result = db_session.get(AnalysisResult, result_id)
    old_json = deepcopy(old_result.result_json)
    service = ExtendAnalysisService(db_session)
    job = service.create_job(result_id, "2022Q1")
    assert job.status == "queued"
    result = service.run(job.id, result_id)
    db_session.refresh(old_result)
    assert result.parent_result_id == result_id
    assert result.version == 2
    assert "delta_summary" in result.result_json
    assert old_result.result_json == old_json
    assert {"added_periods", "changed_metrics", "new_warnings", "resolved_warnings", "data_quality_change"} <= set(
        result.result_json["delta_summary"]
    )
