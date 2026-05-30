import argparse
import json
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.services.parsing.dataframe_statement_parser import DataFrameStatementParser


def run_smoke(
    ticker: str,
    period_from: str,
    period_to: str,
    reporting_standard: str = "IFRS",
    replay_cache: bool = True,
    allow_text_fallback_semantic_gate: bool = False,
) -> dict[str, Any]:
    if not replay_cache:
        raise RuntimeError("DataFrame fact smoke supports replay-cache mode only.")
    init_db()
    with SessionLocal() as db:
        result = DataFrameStatementParser(
            db=db,
            allow_text_fallback_semantic_gate=allow_text_fallback_semantic_gate,
        ).parse(ticker, period_from, period_to, reporting_standard)
    metric_codes = {fact.metric_code for fact in result.facts}
    traceability_complete = all(
        fact.source_document_id
        and fact.source_table_type
        and fact.source_table_index is not None
        and fact.source_location
        and fact.raw_label
        and fact.raw_value
        for fact in result.facts
    )
    notes_fallback_facts = [
        fact
        for fact in result.facts
        if fact.extraction_method == "text_table_fallback_semantic_gate"
        and "notes" in str(fact.source_location.get("source_location", "")).casefold()
    ]
    minimum = (
        ("revenue" in metric_codes or "net_income" in metric_codes)
        and (
            "total_assets" in metric_codes
            or {"current_assets", "current_liabilities"}.issubset(metric_codes)
        )
        and traceability_complete
        and not notes_fallback_facts
    )
    legacy_minimum = (
        "revenue" in metric_codes
        and ("net_income" in metric_codes or "operating_profit" in metric_codes)
        and "total_assets" in metric_codes
        and ("total_equity" in metric_codes or {"current_assets", "current_liabilities"}.issubset(metric_codes))
    )
    report = result.to_dict()
    report["minimum_fact_set"] = {
        "revenue": "revenue" in metric_codes,
        "revenue_or_net_income": bool({"revenue", "net_income"} & metric_codes),
        "net_income_or_operating_profit": bool({"net_income", "operating_profit"} & metric_codes),
        "total_assets": "total_assets" in metric_codes,
        "assets_or_current_ratio_inputs": "total_assets" in metric_codes
        or {"current_assets", "current_liabilities"}.issubset(metric_codes),
        "equity_or_current_ratio_inputs": "total_equity" in metric_codes
        or {"current_assets", "current_liabilities"}.issubset(metric_codes),
        "traceability_complete": traceability_complete,
        "notes_fallback_facts_count": len(notes_fallback_facts),
    }
    report["task_minimum_fact_smoke_passed"] = minimum
    report["legacy_task_minimum_fact_smoke_passed"] = legacy_minimum
    report["db_persisted"] = False
    save_smoke_report(report)
    return report


def save_smoke_report(report: dict[str, Any]) -> Path:
    root = get_settings().root_dir / "data" / "validation" / report["company_ticker"].upper()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{report['period_from']}_{report['period_to']}_dataframe_fact_pipeline_smoke.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test DataFrame statement fact parsing without persistence.")
    parser.add_argument("ticker")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    parser.add_argument("--reporting-standard", default="IFRS", choices=["IFRS", "RAS"])
    parser.add_argument("--replay-cache", action="store_true")
    parser.add_argument("--allow-text-fallback-semantic-gate", action="store_true")
    args = parser.parse_args(argv)
    report = run_smoke(
        args.ticker,
        args.period_from,
        args.period_to,
        args.reporting_standard,
        args.replay_cache,
        args.allow_text_fallback_semantic_gate,
    )
    path = save_smoke_report(report)
    print(
        "\n".join(
            [
                f"company: {report['company_ticker']}",
                f"status: {report['status']}",
                f"canonical_fact_candidates: {report['canonical_facts_created']}",
                f"text_fallback_semantic_gate_enabled: {report['text_fallback_semantic_gate_enabled']}",
                f"task_minimum_fact_smoke_passed: {report['task_minimum_fact_smoke_passed']}",
                f"db_persisted: {report['db_persisted']}",
                f"report_path: {path}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
