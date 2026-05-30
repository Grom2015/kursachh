import argparse

from app.services.demo.demo_pipeline_orchestrator import DemoPipelineOrchestrator, DemoPipelineRequest


def run_demo_pipeline(
    tickers: list[str],
    period_from: str,
    period_to: str,
    reporting_standard: str = "IFRS",
    replay_cache: bool = True,
    output_formats: list[str] | None = None,
) -> tuple[dict, dict[str, str]]:
    orchestrator = DemoPipelineOrchestrator()
    report = orchestrator.build_report(
        DemoPipelineRequest(
            tickers=[ticker.strip().upper() for ticker in tickers if ticker.strip()],
            period_from=period_from,
            period_to=period_to,
            reporting_standard=reporting_standard,
            replay_cache=replay_cache,
            output_formats=output_formats or ["json", "md", "html"],
        )
    )
    paths = orchestrator.save_report(report, output_formats=output_formats or ["json", "md", "html"])
    return report.to_dict(), paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a report-only demo pipeline summary from existing artifacts.")
    parser.add_argument("tickers", help="Comma-separated tickers, e.g. LKOH,TATN,GAZP.")
    parser.add_argument("--period-from", required=True)
    parser.add_argument("--period-to", required=True)
    parser.add_argument("--reporting-standard", default="IFRS", choices=["IFRS", "RAS"])
    parser.add_argument("--replay-cache", action="store_true")
    parser.add_argument("--json-only", action="store_true")
    parser.add_argument("--markdown-only", action="store_true")
    args = parser.parse_args(argv)
    if args.json_only and args.markdown_only:
        parser.error("--json-only and --markdown-only are mutually exclusive")
    output_formats = ["json"] if args.json_only else ["md"] if args.markdown_only else ["json", "md", "html"]
    report, paths = run_demo_pipeline(
        [item.strip().upper() for item in args.tickers.split(",") if item.strip()],
        args.period_from,
        args.period_to,
        reporting_standard=args.reporting_standard,
        replay_cache=args.replay_cache,
        output_formats=output_formats,
    )
    status_by_ticker = {item["ticker"]: item["main_status"] for item in report["company_results"]}
    print(
        "\n".join(
            [
                f"tickers: {','.join(report['tickers'])}",
                f"period: {report['period_from']}..{report['period_to']}",
                f"reporting_standard: {report['reporting_standard']}",
                f"statuses: {status_by_ticker}",
                f"json_report_path: {paths.get('json')}",
                f"markdown_report_path: {paths.get('md')}",
                f"html_report_path: {paths.get('html')}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
