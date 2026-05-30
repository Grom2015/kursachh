from app.services.analysis.orchestrator import AnalysisOrchestrator


def test_smoke_api_flow(client, db_session, monkeypatch):
    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert health["app_version"]
    assert health["build_id"]
    assert health["started_at"]
    assert health["process_id"]
    search = client.get("/companies/search", params={"q": "Лукойл"})
    assert search.status_code == 200
    assert search.json()["items"][0]["ticker"] == "LKOH"

    create = client.post(
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
    assert create.status_code == 200
    job_id = create.json()["job_id"]
    # TestClient dependency override uses the test session, while the production background
    # task opens SessionLocal. Run the same orchestrator step explicitly for test isolation.
    AnalysisOrchestrator(db_session).run(job_id)
    job = client.get(f"/analysis-jobs/{job_id}").json()
    assert job["status"] == "succeeded"
    assert job["result_id"]

    result = client.get(f"/analysis-results/{job['result_id']}").json()
    assert result["company"]["ticker"] == "LKOH"
    assert result["result"]["period"]
    assert result["result"]["financial_analysis"]
    assert result["result"]["market_analysis"]
    assert result["result"]["peer_analysis"]
    assert result["result"]["source_documents"]
    assert result["result"]["warnings"]
    assert result["disclaimer"]
    assert result["llm_payload"]

    history = client.get("/analysis-history", params={"company": "LKOH"}).json()
    assert history["items"]

    monkeypatch.setattr("app.api.routes.analysis.run_extend_job", lambda job_id, parent_result_id: None)
    extend = client.post(
        f"/analysis-results/{job['result_id']}/extend",
        json={"new_period_to": "2022Q1", "force_refetch": False},
    )
    assert extend.status_code == 200
    from app.services.analysis.extend_service import ExtendAnalysisService

    ExtendAnalysisService(db_session).run(extend.json()["job_id"], job["result_id"])
    extend_job = client.get(f"/analysis-jobs/{extend.json()['job_id']}").json()
    assert extend_job["status"] == "succeeded"
