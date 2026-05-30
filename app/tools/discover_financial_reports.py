import argparse

from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.services.reports.financial_report_discovery import (
    FinancialReportDiscoveryRequest,
    FinancialReportDiscoveryService,
)


def run_discovery(
    company_query: str,
    period_from: str,
    period_to: str,
    reporting_standard: str = "IFRS",
    live: bool = False,
) -> tuple[dict, str]:
    init_db()
    with SessionLocal() as db:
        service = FinancialReportDiscoveryService(db)
        report = service.discover(
            FinancialReportDiscoveryRequest(
                company_query=company_query,
                ticker=company_query.upper(),
                period_from=period_from,
                period_to=period_to,
                reporting_standard=reporting_standard,
                live=live,
            )
        )
        path = service.save_report(report)
        return report.to_dict(), str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover financial report candidates without downloading or parsing.")
    parser.add_argument("company")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    parser.add_argument("--reporting-standard", default="IFRS", choices=["IFRS", "RAS"])
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    report, path = run_discovery(args.company, args.period_from, args.period_to, args.reporting_standard, args.live)
    print(
        "\n".join(
            [
                f"company: {(report.get('resolved_company') or {}).get('ticker', args.company.upper())}",
                f"status: {report['status']}",
                f"discovered_reports: {len(report['discovered_reports'])}",
                f"missing_periods: {', '.join(report['missing_periods']) or 'none'}",
                "downloaded_documents_count: 0",
                f"report_path: {path}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
