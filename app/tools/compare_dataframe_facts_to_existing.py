import argparse
import json
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.services.parsing.dataframe_fact_comparison import compare_dataframe_facts_to_existing


def run_comparison(
    ticker: str,
    period_from: str,
    period_to: str,
    reporting_standard: str = "IFRS",
    replay_cache: bool = True,
    allow_text_fallback_semantic_gate: bool = False,
) -> dict[str, Any]:
    if not replay_cache:
        raise RuntimeError("DataFrame fact comparison supports replay-cache mode only.")
    init_db()
    with SessionLocal() as db:
        report = compare_dataframe_facts_to_existing(
            db,
            ticker,
            period_from,
            period_to,
            reporting_standard=reporting_standard,
            replay_cache=replay_cache,
            allow_text_fallback_semantic_gate=allow_text_fallback_semantic_gate,
        )
    save_report(report)
    return report


def save_report(report: dict[str, Any]) -> Path:
    root = get_settings().root_dir / "data" / "validation" / report["company"].upper()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{report['period_from']}_{report['period_to']}_dataframe_vs_existing_fact_comparison.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit-only comparison of DataFrame fact candidates with existing facts.")
    parser.add_argument("ticker")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    parser.add_argument("--reporting-standard", default="IFRS", choices=["IFRS", "RAS"])
    parser.add_argument("--replay-cache", action="store_true")
    parser.add_argument("--allow-text-fallback-semantic-gate", action="store_true")
    args = parser.parse_args(argv)
    report = run_comparison(
        args.ticker,
        args.period_from,
        args.period_to,
        reporting_standard=args.reporting_standard,
        replay_cache=args.replay_cache,
        allow_text_fallback_semantic_gate=args.allow_text_fallback_semantic_gate,
    )
    path = save_report(report)
    print(
        "\n".join(
            [
                f"company: {report['company']}",
                f"dataframe_candidates_count: {report['dataframe_candidates_count']}",
                f"existing_facts_count: {report['existing_facts_count']}",
                f"matched_count: {report['matched_count']}",
                f"same_value_count: {report['same_value_count']}",
                f"missing_in_existing_count: {report['missing_in_existing_count']}",
                f"missing_in_dataframe_count: {report['missing_in_dataframe_count']}",
                f"conflict_count: {report['conflict_count']}",
                f"safe_to_persist_in_this_stage: {report['safe_to_persist_in_this_stage']}",
                f"persist_eligibility: {report['persist_eligibility']}",
                f"report_path: {path}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
