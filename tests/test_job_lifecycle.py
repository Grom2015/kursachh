from app.db.models import AnalysisJob, AnalysisResult
from app.services.analysis.orchestrator import AnalysisOrchestrator


def test_failed_job_company_not_found(db_session):
    job = AnalysisJob(
        company_query="Not A Company",
        period_from="2021Q2",
        period_to="2021Q4",
        reporting_standard="IFRS",
    )
    db_session.add(job)
    db_session.commit()
    AnalysisOrchestrator(db_session).run(job.id)
    db_session.refresh(job)
    assert job.status == "failed"
    assert "Company not found" in job.error_message
    assert job.stage == "resolving_company"
    assert job.result_id is None


def test_failed_job_invalid_period(db_session):
    job = AnalysisJob(
        company_query="LKOH",
        period_from="2021Q5",
        period_to="2021Q4",
        reporting_standard="IFRS",
    )
    db_session.add(job)
    db_session.commit()
    AnalysisOrchestrator(db_session).run(job.id)
    db_session.refresh(job)
    assert job.status == "failed"
    assert "Invalid period format" in job.error_message
    assert job.result_id is None


def test_failed_job_parser_failure(monkeypatch, db_session):
    def boom(self, document):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr("app.services.parsing.csv_parser.CSVParser.parse", boom)
    job = AnalysisJob(
        company_query="LKOH",
        period_from="2021Q2",
        period_to="2021Q4",
        reporting_standard="IFRS",
    )
    db_session.add(job)
    db_session.commit()
    AnalysisOrchestrator(db_session).run(job.id)
    db_session.refresh(job)
    assert job.status == "failed"
    assert "parser exploded" in job.error_message
    assert job.stage == "parsing_reports"
    assert job.result_id is None


def test_succeeded_job_has_result_and_job_link(db_session):
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
    assert job.status == "queued"
    AnalysisOrchestrator(db_session).run(job.id)
    db_session.refresh(job)
    assert job.status == "succeeded"
    assert job.stage == "completed"
    assert job.progress == 1.0
    assert job.result_id
    result = db_session.get(AnalysisResult, job.result_id)
    assert result is not None
    assert result.job_id == job.id
