import argparse

from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.services.coverage.universal_coverage_scanner import CoverageScanRequest, UniversalCoverageScanner


def scan_universal_coverage(
    period_from: str,
    period_to: str,
    reporting_standard: str = "IFRS",
    tickers: list[str] | None = None,
    from_registry: bool = False,
    limit: int | None = None,
    replay_cache: bool = True,
    run_missing_table_extraction: bool = False,
    allow_text_fallback_semantic_gate: bool = False,
) -> tuple[dict, str]:
    init_db()
    with SessionLocal() as db:
        scanner = UniversalCoverageScanner(db)
        report = scanner.scan(
            CoverageScanRequest(
                tickers=tickers,
                from_registry=from_registry,
                limit=limit,
                period_from=period_from,
                period_to=period_to,
                reporting_standard=reporting_standard,
                replay_cache=replay_cache,
                run_missing_table_extraction=run_missing_table_extraction,
                allow_text_fallback_semantic_gate=allow_text_fallback_semantic_gate,
            )
        )
        path = scanner.save_report(report)
        return report.to_dict(), str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan universal real-data coverage without running analysis.")
    parser.add_argument("--from-registry", action="store_true")
    parser.add_argument("--tickers")
    parser.add_argument("--period-from", required=True)
    parser.add_argument("--period-to", required=True)
    parser.add_argument("--reporting-standard", default="IFRS", choices=["IFRS", "RAS"])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--replay-cache", action="store_true")
    parser.add_argument("--run-missing-table-extraction", action="store_true")
    parser.add_argument("--allow-text-fallback-semantic-gate", action="store_true")
    args = parser.parse_args(argv)
    tickers = [item.strip().upper() for item in args.tickers.split(",")] if args.tickers else None
    report, path = scan_universal_coverage(
        args.period_from,
        args.period_to,
        args.reporting_standard,
        tickers=tickers,
        from_registry=args.from_registry,
        limit=args.limit,
        replay_cache=args.replay_cache,
        run_missing_table_extraction=args.run_missing_table_extraction,
        allow_text_fallback_semantic_gate=args.allow_text_fallback_semantic_gate,
    )
    summary = report["summary"]
    print(
        "\n".join(
            [
                f"companies_scanned: {report['companies_scanned']}",
                f"scan_mode: {report['scan_mode']}",
                f"artifacts_created: {report['artifacts_created']}",
                f"FULL_STATEMENT_READY: {summary['coverage_level_counts'].get('FULL_STATEMENT_READY', 0)}",
                f"PARTIAL_STATEMENT_READY: {summary['coverage_level_counts'].get('PARTIAL_STATEMENT_READY', 0)}",
                f"SOURCE_BLOCKED: {summary['coverage_level_counts'].get('SOURCE_BLOCKED', 0)}",
                f"PARSER_BLOCKED: {summary['coverage_level_counts'].get('PARSER_BLOCKED', 0)}",
                f"UNSUPPORTED: {summary['coverage_level_counts'].get('UNSUPPORTED', 0)}",
                f"report_path: {path}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
