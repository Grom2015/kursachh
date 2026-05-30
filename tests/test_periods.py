import pytest

from app.db.models import AnalysisJob
from app.services.analysis.extend_service import ExtendAnalysisService
from app.services.analysis.orchestrator import AnalysisOrchestrator
from app.services.periods import ensure_period_range, period_or_year_in_range


def test_invalid_quarter_rejected():
    with pytest.raises(ValueError, match="Invalid period format"):
        ensure_period_range("2021Q5", "2022Q1")


def test_invalid_date_like_period_rejected():
    with pytest.raises(ValueError, match="Invalid period format"):
        ensure_period_range("2021-02", "2022Q1")


def test_2021q4_to_2022q1_accepted():
    ensure_period_range("2021Q4", "2022Q1")


def test_period_or_year_in_range_treats_plain_year_as_full_year():
    assert period_or_year_in_range("2025", "2025Q1", "2025Q4")
    assert period_or_year_in_range("2025", "2024Q4", "2025Q1")
    assert not period_or_year_in_range("2025", "2026Q1", "2026Q4")


def _create_result(db_session, period_to="2025Q4"):
    job = AnalysisJob(
        company_query="LKOH",
        period_from="2021Q2",
        period_to=period_to,
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


def test_extend_same_period_rejected(db_session):
    result_id = _create_result(db_session, "2025Q4")
    with pytest.raises(ValueError, match="greater"):
        ExtendAnalysisService(db_session).create_job(result_id, "2025Q4")


def test_extend_earlier_period_rejected(db_session):
    result_id = _create_result(db_session, "2025Q4")
    with pytest.raises(ValueError, match="greater"):
        ExtendAnalysisService(db_session).create_job(result_id, "2025Q3")
