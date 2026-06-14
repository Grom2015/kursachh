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
    assert "report_markdown" in payload
    assert db_session.get(AnalysisResult, job.result_id) is not None


def test_analysis_report_endpoint_returns_sections(client, db_session):
    job = AnalysisJob(
        company_query="LKOH",
        period_from="2021Q1",
        period_to="2021Q4",
        reporting_standard="IFRS",
        status="succeeded",
    )
    db_session.add(job)
    db_session.flush()

    result = AnalysisResult(
        job_id=job.id,
        company_id=1,
        period_from="2021Q1",
        period_to="2021Q4",
        reporting_standard="IFRS",
        data_snapshot_json={},
        result_json={
            "llm_report": {
                "fundamental_note": "fundamental",
                "technical_note": "technical",
                "peer_note": "peer",
                "overall_summary": "overall",
                "recommendation": "HOLD",
                "llm_model": "claude-test",
                "token_usage": {"total_tokens": 123},
            }
        },
        llm_payload_json={},
        report_markdown="# Report",
        warnings_json=[],
        disclaimer="demo disclaimer",
    )
    db_session.add(result)
    db_session.commit()

    response = client.get(f"/analysis-results/{result.id}/report")
    assert response.status_code == 200
    payload = response.json()
    assert payload["report_markdown"] == "# Report"
    assert payload["recommendation"] == "HOLD"
    assert payload["llm_model"] == "claude-test"


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
