from sqlalchemy.orm import Session

from app.db.models import AnalysisJob, AnalysisResult
from app.services.analysis.orchestrator import AnalysisOrchestrator
from app.services.periods import ensure_extension_period


class ExtendAnalysisService:
    def __init__(self, db: Session):
        self.db = db

    def create_job(self, result_id: str, new_period_to: str) -> AnalysisJob:
        previous = self.db.get(AnalysisResult, result_id)
        if not previous:
            raise ValueError("AnalysisResult not found")
        ensure_extension_period(previous.period_to, new_period_to)
        job = AnalysisJob(
            user_id=previous.user_id,
            company_id=previous.company_id,
            company_query=previous.company.ticker,
            period_from=previous.period_from,
            period_to=new_period_to,
            reporting_standard=previous.reporting_standard,
            include_market_data=True,
            include_peers=True,
            include_news=False,
            data_mode=previous.data_snapshot_json.get("data_mode", "fixture"),
            status="queued",
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    def run(self, job_id: str, parent_result_id: str) -> AnalysisResult | None:
        previous = self.db.get(AnalysisResult, parent_result_id)
        if not previous:
            raise ValueError("AnalysisResult not found")
        result = AnalysisOrchestrator(self.db).run(
            job_id, parent_result_id=parent_result_id, version=previous.version + 1
        )
        if result:
            delta = self.build_delta(previous.result_json, result.result_json)
            result.result_json = {**result.result_json, "delta_summary": delta}
            self.db.commit()
        return result

    def build_delta(self, old: dict, new: dict) -> dict:
        old_metrics = {
            (item["metric_code"], item["period"]): item
            for item in old.get("financial_analysis", {}).get("metrics", [])
        }
        new_metrics = {
            (item["metric_code"], item["period"]): item
            for item in new.get("financial_analysis", {}).get("metrics", [])
        }
        added = [item for key, item in new_metrics.items() if key not in old_metrics]
        changed = []
        for key, item in new_metrics.items():
            if key not in old_metrics:
                continue
            old_value = old_metrics[key].get("value")
            new_value = item.get("value")
            if old_value is not None and new_value is not None and old_value != new_value:
                changed.append(
                    {
                        "metric_code": key[0],
                        "period": key[1],
                        "old_value": old_value,
                        "new_value": new_value,
                        "absolute_change": new_value - old_value,
                        "percentage_change": None if old_value == 0 else (new_value - old_value) / old_value,
                    }
                )
        old_warnings = set(old.get("financial_analysis", {}).get("warnings", []))
        new_warnings = set(new.get("financial_analysis", {}).get("warnings", []))
        return {
            "added_periods": sorted({item["period"] for item in added}),
            "added_metrics": added,
            "changed_metrics": changed,
            "new_warnings": sorted(new_warnings - old_warnings),
            "resolved_warnings": sorted(old_warnings - new_warnings),
            "data_quality_change": {
                "old": old.get("data_quality", {}).get("overall_quality"),
                "new": new.get("data_quality", {}).get("overall_quality"),
                "analytical_note": "Новые данные обновили расчетные метрики; выводы остаются информационно-аналитическими.",
            },
        }


def run_extend_job(job_id: str, parent_result_id: str) -> None:
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        ExtendAnalysisService(db).run(job_id, parent_result_id)
