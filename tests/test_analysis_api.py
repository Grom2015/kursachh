from app.db.models import AnalysisJob, AnalysisResult
from app.services.analysis.orchestrator import AnalysisOrchestrator


def test_analysis_job_and_result_flow(client, db_session):
    response = client.post(
        "/analysis-jobs",
        json={
            "company_query": "Лукойл",
            "period_from": "2021Q2",
            "period_to": "2021Q4",
            "reporting_standard": "IFRS",
            "include_market_data": True,
            "include_peers": True,
            "include_news": False,
            "output_language": "ru",
        },
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    job_response = client.get(f"/analysis-jobs/{job_id}")
    assert job_response.status_code == 200

    AnalysisOrchestrator(db_session).run(job_id)
    job = db_session.get(AnalysisJob, job_id)
    assert job.status == "succeeded"
    assert job.result_id

    result_response = client.get(f"/analysis-results/{job.result_id}")
    assert result_response.status_code == 200
    payload = result_response.json()
    assert payload["result"]["disclaimer"]
    assert payload["llm_payload"]["task"] == "prepare_financial_analytics_note"
    assert db_session.get(AnalysisResult, job.result_id) is not None


def test_invalid_period_api_error_is_clear(client):
    response = client.post(
        "/analysis-jobs",
        json={
            "company_query": "Лукойл",
            "period_from": "2021Q5",
            "period_to": "2021Q4",
            "reporting_standard": "IFRS",
        },
    )
    assert response.status_code == 422
    assert "Invalid period format" in response.json()["detail"]
