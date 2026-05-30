import argparse

from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.services.reports.manual_report_ingestion import ManualReportIngestionRequest, ManualReportIngestionService


def ingest_manual_report(
    company_ticker: str,
    period: str,
    reporting_standard: str,
    file_path: str,
    original_source_url: str | None = None,
    document_type: str = "financial_statements",
    source_role: str | None = None,
    manual_upload_reason: str = "user_requested",
    run_dataframe_fact_parser: bool = False,
    allow_text_fallback_semantic_gate: bool = False,
) -> tuple[dict, str]:
    init_db()
    with SessionLocal() as db:
        service = ManualReportIngestionService(db)
        report = service.ingest(
            ManualReportIngestionRequest(
                company_ticker=company_ticker,
                period=period,
                reporting_standard=reporting_standard,
                local_file_path=file_path,
                original_source_url=original_source_url,
                document_type=document_type,
                source_role=source_role,
                manual_upload_reason=manual_upload_reason,
                run_dataframe_fact_parser=run_dataframe_fact_parser,
                allow_text_fallback_semantic_gate=allow_text_fallback_semantic_gate,
            )
        )
        path = service.save_report(report)
        return report.to_dict(), str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest a local official report as a no-API manual fallback.")
    parser.add_argument("company_ticker")
    parser.add_argument("period")
    parser.add_argument("--reporting-standard", default="IFRS", choices=["IFRS", "RAS"])
    parser.add_argument("--file", required=True)
    parser.add_argument("--original-source-url")
    parser.add_argument("--document-type", default="financial_statements")
    parser.add_argument("--source-role")
    parser.add_argument(
        "--manual-upload-reason",
        default="user_requested",
        choices=["api_unavailable", "source_discovery_failed", "missing_fy_report", "manual_test", "user_requested"],
    )
    parser.add_argument("--run-dataframe-fact-parser", action="store_true")
    parser.add_argument("--allow-text-fallback-semantic-gate", action="store_true")
    args = parser.parse_args(argv)
    report, path = ingest_manual_report(
        args.company_ticker,
        args.period,
        args.reporting_standard,
        args.file,
        original_source_url=args.original_source_url,
        document_type=args.document_type,
        source_role=args.source_role,
        manual_upload_reason=args.manual_upload_reason,
        run_dataframe_fact_parser=args.run_dataframe_fact_parser,
        allow_text_fallback_semantic_gate=args.allow_text_fallback_semantic_gate,
    )
    print(
        "\n".join(
            [
                f"status: {report['status']}",
                f"report_document_id: {report['report_document_id']}",
                f"duplicate_detected: {report['duplicate_detected']}",
                f"document_validation_status: {report['document_validation_status']}",
                f"statement_tables_extracted: {report['statement_tables_extracted']}",
                f"fact_parse_status: {report['fact_parse_status']}",
                f"db_persisted: {report['db_persisted']}",
                f"source_trust_bucket: {report['source_trust_bucket']}",
                f"official_source_verified: {report['official_source_verified']}",
                f"source_package_ready_contribution: {report['source_package_ready_contribution']}",
                f"report_path: {path}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
