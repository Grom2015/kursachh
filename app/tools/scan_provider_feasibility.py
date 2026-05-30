import argparse

from app.services.providers.provider_feasibility_scanner import ProviderFeasibilityRequest, ProviderFeasibilityScanner


def scan_provider_feasibility(
    market: str = "MOEX",
    offline_only: bool = True,
    live_metadata_check: bool = False,
    providers: list[str] | None = None,
    reporting_standards: list[str] | None = None,
) -> tuple[dict, str]:
    scanner = ProviderFeasibilityScanner()
    report = scanner.scan(
        ProviderFeasibilityRequest(
            market=market,
            providers=providers,
            reporting_standards=reporting_standards or ["IFRS", "RAS"],
            offline_only=offline_only,
            live_metadata_check=live_metadata_check,
        )
    )
    path = scanner.save_report(report)
    return report.to_dict(), str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan external provider feasibility without production side effects.")
    parser.add_argument("--market", default="MOEX")
    parser.add_argument("--offline-only", action="store_true")
    parser.add_argument("--live-metadata-check", action="store_true")
    parser.add_argument("--providers", help="Comma-separated provider names from the curated catalog.")
    parser.add_argument("--reporting-standards", default="IFRS,RAS")
    args = parser.parse_args(argv)
    providers = [item.strip() for item in args.providers.split(",") if item.strip()] if args.providers else None
    reporting_standards = [item.strip().upper() for item in args.reporting_standards.split(",") if item.strip()]
    offline_only = args.offline_only or not args.live_metadata_check
    report, path = scan_provider_feasibility(
        market=args.market,
        offline_only=offline_only,
        live_metadata_check=args.live_metadata_check,
        providers=providers,
        reporting_standards=reporting_standards,
    )
    summary = report["summary"]
    print(
        "\n".join(
            [
                f"providers_checked: {report['providers_checked']}",
                f"offline_only: {report['offline_only']}",
                f"live_metadata_check: {report['live_metadata_check']}",
                f"api_available_counts: {summary['api_available_counts']}",
                f"production_ready_count: {summary['production_ready_count']}",
                f"requires_access_verification_count: {summary['requires_access_verification_count']}",
                f"report_path: {path}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
