import argparse

from app.services.reports.moex_top50_catalog_checker import CatalogDownloadCheckRequest, MoexTop50CatalogChecker


def run_check(
    period_from: str,
    period_to: str,
    reporting_standard: str = "IFRS",
    limit: int | None = None,
    download: bool = True,
    max_downloads_per_company: int = 4,
) -> tuple[dict, str]:
    checker = MoexTop50CatalogChecker()
    report = checker.run(
        CatalogDownloadCheckRequest(
            period_from=period_from,
            period_to=period_to,
            reporting_standard=reporting_standard,
            limit=limit,
            download=download,
            max_downloads_per_company=max_downloads_per_company,
        )
    )
    path = checker.save_report(report)
    return report, str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check official report source catalog pages and optionally download/validate report candidates."
    )
    parser.add_argument("--period-from", default="2025Q1")
    parser.add_argument("--period-to", default="2025Q4")
    parser.add_argument("--reporting-standard", default="IFRS", choices=["IFRS", "RAS"])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--max-downloads-per-company", type=int, default=4)
    parser.add_argument("--no-download", action="store_true")
    args = parser.parse_args(argv)
    report, path = run_check(
        period_from=args.period_from,
        period_to=args.period_to,
        reporting_standard=args.reporting_standard,
        limit=args.limit,
        download=not args.no_download,
        max_downloads_per_company=args.max_downloads_per_company,
    )
    summary = report["summary"]
    print(
        "\n".join(
            [
                f"companies_checked: {report['companies_checked']}",
                f"pass_count: {summary['pass_count']}",
                f"partial_count: {summary['partial_count']}",
                f"blocked_count: {summary['blocked_count']}",
                f"pages_reachable_count: {summary['pages_reachable_count']}",
                f"report_candidates_found_count: {summary['report_candidates_found_count']}",
                f"documents_downloaded_count: {summary['documents_downloaded_count']}",
                f"documents_validated_count: {summary['documents_validated_count']}",
                f"report_path: {path}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
