import argparse

from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.services.metrics.financial_ratios_calculator import FinancialRatiosCalculator, FinancialRatiosRequest


def calculate_financial_ratios(
    ticker: str,
    period_from: str,
    period_to: str,
    reporting_standard: str = "IFRS",
    source: str = "persisted_facts",
    allow_text_fallback_candidates: bool = False,
) -> tuple[dict, str]:
    init_db()
    with SessionLocal() as db:
        calculator = FinancialRatiosCalculator(db=db)
        report = calculator.calculate(
            FinancialRatiosRequest(
                company_ticker=ticker,
                period_from=period_from,
                period_to=period_to,
                reporting_standard=reporting_standard,
                source=source,
                allow_text_fallback_candidates=allow_text_fallback_candidates,
            )
        )
        path = calculator.save_report(report)
        return report.to_dict(), str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Calculate report-only financial ratios from normalized facts.")
    parser.add_argument("ticker")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    parser.add_argument("--reporting-standard", default="IFRS", choices=["IFRS", "RAS"])
    parser.add_argument("--from-dataframe-parse-report", action="store_true")
    parser.add_argument("--allow-text-fallback-candidates", action="store_true")
    parser.add_argument("--json-only", action="store_true")
    args = parser.parse_args(argv)
    source = "dataframe_parse_report" if args.from_dataframe_parse_report else "persisted_facts"
    report, path = calculate_financial_ratios(
        args.ticker,
        args.period_from,
        args.period_to,
        reporting_standard=args.reporting_standard,
        source=source,
        allow_text_fallback_candidates=args.allow_text_fallback_candidates,
    )
    if not args.json_only:
        summary = report["summary"]
        print(
            "\n".join(
                [
                    f"company: {report['company_ticker']}",
                    f"period: {report['period_from']}..{report['period_to']}",
                    f"facts_source: {report['facts_source']}",
                    f"calculated_count: {summary['calculated_count']}",
                    f"missing_count: {summary['missing_count']}",
                    f"unsupported_count: {summary['unsupported_count']}",
                    f"blocked_count: {summary['blocked_count']}",
                    f"report_path: {path}",
                ]
            )
        )
    else:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
