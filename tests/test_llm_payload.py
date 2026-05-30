from app.db.models import AnalysisJob
from app.services.analysis.orchestrator import AnalysisOrchestrator


def test_llm_payload_has_compliance_and_no_advice_wording(db_session):
    job = AnalysisJob(
        company_query="LKOH",
        period_from="2021Q2",
        period_to="2021Q4",
        reporting_standard="IFRS",
        include_market_data=True,
        include_peers=True,
    )
    db_session.add(job)
    db_session.commit()
    result = AnalysisOrchestrator(db_session).run(job.id)
    payload = result.llm_payload_json
    serialized = str(payload).casefold()
    forbidden = ["buy", "sell", "hold", "покупать", "продавать", "держать"]
    assert not any(word in serialized for word in forbidden)
    assert payload["compliance"]["not_individual_investment_recommendation"] is True
    assert payload["compliance"]["disclaimer"]
    assert payload["instructions_for_llm"]
    assert payload["source_documents"]
    assert "fixture" in payload["data_quality_flags"]
    assert any("Fixture/demo data" in warning for warning in payload["warnings"])


def test_fixture_values_are_marked_in_result_json(db_session):
    job = AnalysisJob(
        company_query="LKOH",
        period_from="2021Q2",
        period_to="2021Q4",
        reporting_standard="IFRS",
        include_market_data=True,
        include_peers=True,
    )
    db_session.add(job)
    db_session.commit()
    result = AnalysisOrchestrator(db_session).run(job.id)
    result_json = result.result_json
    assert "fixture" in result_json["data_quality"]["flags"]
    assert any(document["source_type"] == "fixture" for document in result_json["source_documents"])
    assert all(
        fact["quality_flag"] == "fixture" and fact["source_type"] == "fixture"
        for fact in result_json["financial_analysis"]["facts_summary"]
    )
    assert any(metric["quality_flag"] == "fixture" for metric in result_json["financial_analysis"]["metrics"])
    assert result_json["warnings"]

