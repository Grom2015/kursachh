import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.services.parsing.dataframe_statement_parser import DataFrameStatementParser


def parse_statement_tables_to_facts(
    ticker: str,
    period_from: str,
    period_to: str,
    reporting_standard: str = "IFRS",
    replay_cache: bool = True,
    persist: bool = False,
    allow_text_fallback_semantic_gate: bool = False,
) -> dict[str, Any]:
    if not replay_cache:
        raise RuntimeError("DataFrame statement parser supports replay-cache mode only.")
    init_db()
    with SessionLocal() as db:
        parser = DataFrameStatementParser(
            db=db,
            allow_text_fallback_semantic_gate=allow_text_fallback_semantic_gate,
        )
        result = parser.parse(ticker, period_from, period_to, reporting_standard)
        persisted_count = parser.persist(result) if persist else 0
    report = result.to_dict()
    report["db_persisted"] = persist
    report["persisted_facts_count"] = persisted_count
    report["text_fallback_semantic_gate_enabled"] = allow_text_fallback_semantic_gate
    report["facts_by_metric_code"] = dict(Counter(fact["metric_code"] for fact in report["facts"]))
    report["rejection_reasons"] = dict(Counter(item["reason"] for item in report["rejected_candidates"]))
    save_parse_report(report)
    return report


def save_parse_report(report: dict[str, Any]) -> Path:
    root = get_settings().root_dir / "data" / "validation" / report["company_ticker"].upper()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{report['period_from']}_{report['period_to']}_dataframe_statement_fact_parse.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse saved statement table DataFrames into canonical fact candidates.")
    parser.add_argument("ticker")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    parser.add_argument("--reporting-standard", default="IFRS", choices=["IFRS", "RAS"])
    parser.add_argument("--replay-cache", action="store_true")
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--allow-text-fallback-semantic-gate", action="store_true")
    args = parser.parse_args(argv)
    report = parse_statement_tables_to_facts(
        args.ticker,
        args.period_from,
        args.period_to,
        args.reporting_standard,
        replay_cache=args.replay_cache,
        persist=args.persist,
        allow_text_fallback_semantic_gate=args.allow_text_fallback_semantic_gate,
    )
    path = save_parse_report(report)
    print(
        "\n".join(
            [
                f"company: {report['company_ticker']}",
                f"status: {report['status']}",
                f"documents_processed: {report['documents_processed']}",
                f"tables_processed: {report['tables_processed']}",
                f"canonical_fact_candidates: {report['canonical_facts_created']}",
                f"text_fallback_semantic_gate_enabled: {report['text_fallback_semantic_gate_enabled']}",
                f"text_fallback_facts_created: {report['text_fallback_facts_created']}",
                f"rejected_candidates: {len(report['rejected_candidates'])}",
                f"db_persisted: {report['db_persisted']}",
                f"persisted_facts_count: {report['persisted_facts_count']}",
                f"report_path: {path}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
