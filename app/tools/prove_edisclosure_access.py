import argparse

from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.services.providers.edisclosure_proof_of_access import EDisclosureProofRequest, EDisclosureProofScanner


def prove_edisclosure_access(
    sample_tickers: list[str] | None = None,
    year: int = 2021,
    reporting_standard: str = "IFRS",
    offline_only: bool = True,
    live_public_docs_check: bool = False,
) -> tuple[dict, str]:
    init_db()
    with SessionLocal() as db:
        scanner = EDisclosureProofScanner(db)
        report = scanner.prove(
            EDisclosureProofRequest(
                sample_tickers=sample_tickers or ["TATN", "NVTK", "ROSN", "SIBN"],
                year=year,
                reporting_standard=reporting_standard,
                offline_only=offline_only,
                live_public_docs_check=live_public_docs_check,
            )
        )
        path = scanner.save_report(report)
        return report.to_dict(), str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prove E-Disclosure access requirements without authenticated calls.")
    parser.add_argument("--offline-only", action="store_true")
    parser.add_argument("--live-public-docs-check", action="store_true")
    parser.add_argument("--sample-tickers", default="TATN,NVTK,ROSN,SIBN")
    parser.add_argument("--year", type=int, default=2021)
    parser.add_argument("--reporting-standard", default="IFRS")
    args = parser.parse_args(argv)
    tickers = [item.strip().upper() for item in args.sample_tickers.split(",") if item.strip()]
    offline_only = args.offline_only or not args.live_public_docs_check
    report, path = prove_edisclosure_access(
        sample_tickers=tickers,
        year=args.year,
        reporting_standard=args.reporting_standard,
        offline_only=offline_only,
        live_public_docs_check=args.live_public_docs_check,
    )
    print(
        "\n".join(
            [
                f"provider_name: {report['provider_name']}",
                f"access_status: {report['access_status']}",
                f"api_documentation_status: {report['api_documentation_status']}",
                f"api_documentation_live_verified: {report['api_documentation_live_verified']}",
                f"production_readiness_status: {report['production_readiness_status']}",
                f"recommended_next_action: {report['recommended_next_action']}",
                f"report_path: {path}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
