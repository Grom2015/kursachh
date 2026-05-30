import sys

from app.db.init_db import init_db
from app.db.models import AnalysisJob, AnalysisResult
from app.db.session import SessionLocal
from app.services.analysis.orchestrator import AnalysisOrchestrator


def _run(db, ticker: str, period_from: str, period_to: str, data_mode: str) -> AnalysisJob:
    job = AnalysisJob(
        company_query=ticker,
        period_from=period_from,
        period_to=period_to,
        reporting_standard="IFRS",
        include_market_data=False,
        include_peers=False,
        include_news=False,
        data_mode=data_mode,
    )
    db.add(job)
    db.commit()
    AnalysisOrchestrator(db).run(job.id)
    db.refresh(job)
    return job


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if len(argv) != 3:
        print("Usage: python -m app.tools.compare_fixture_real LKOH 2021Q1 2021Q4")
        return 2
    ticker, period_from, period_to = argv
    init_db()
    with SessionLocal() as db:
        fixture_job = _run(db, ticker, period_from, period_to, "fixture")
        real_job = _run(db, ticker, period_from, period_to, "real")
        if real_job.status != "succeeded":
            print(f"Real pipeline unavailable: {real_job.error_message}")
            return 0
        fixture_result = db.get(AnalysisResult, fixture_job.result_id)
        real_result = db.get(AnalysisResult, real_job.result_id)
        fixture_metrics = _metrics(fixture_result.result_json)
        real_metrics = _metrics(real_result.result_json)
        print("metric_code\tperiod\tfixture_value\treal_value\tabs_diff\tpct_diff\tfixture_quality\treal_quality")
        for key in sorted(set(fixture_metrics) | set(real_metrics)):
            fm = fixture_metrics.get(key)
            rm = real_metrics.get(key)
            fv = fm.get("value") if fm else None
            rv = rm.get("value") if rm else None
            abs_diff = None if fv is None or rv is None else rv - fv
            pct_diff = None if not fv or rv is None else (rv - fv) / fv
            print(
                f"{key[0]}\t{key[1]}\t{fv}\t{rv}\t{abs_diff}\t{pct_diff}\t"
                f"{fm.get('quality_flag') if fm else None}\t{rm.get('quality_flag') if rm else None}"
            )
    return 0


def _metrics(result_json: dict) -> dict[tuple[str, str], dict]:
    return {
        (item["metric_code"], item["period"]): item
        for item in result_json.get("financial_analysis", {}).get("metrics", [])
    }


if __name__ == "__main__":
    raise SystemExit(main())
