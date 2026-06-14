import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.db.init_db import init_db
from app.db.models import Company, ReportDocument
from app.db.session import SessionLocal
from app.services.parsing.pdf_auto_parse_orchestrator import augment_statement_table_report_with_engine_candidates
from app.services.parsing.statement_table_extractor import (
    StatementTableExtractor,
    required_statement_tables_missing,
    statement_coverage,
)
from app.services.periods import period_in_range


def extract_statement_tables(
    ticker: str,
    period_from: str,
    period_to: str,
    reporting_standard: str = "IFRS",
    replay_cache: bool = True,
) -> dict[str, Any]:
    if not replay_cache:
        raise RuntimeError("Only replay-cache mode is supported by statement table extraction CLI.")
    init_db()
    ticker = ticker.upper()
    with SessionLocal() as db:
        company = db.scalar(select(Company).where(Company.ticker == ticker))
        if not company:
            raise RuntimeError(f"Company not found: {ticker}")
        docs = [
            doc
            for doc in db.scalars(
                select(ReportDocument).where(
                    ReportDocument.company_id == company.id,
                    ReportDocument.reporting_standard == reporting_standard.upper(),
                    ReportDocument.source_type != "fixture",
                    ReportDocument.status.in_(["downloaded", "parsed", "validated", "evidence_only"]),
                )
            ).all()
            if period_in_range(doc.report_period, period_from, period_to)
            and _usable_statement_role(doc.source_role)
        ]
        docs = _dedupe_documents(docs)
        extractor = StatementTableExtractor()
        root = get_settings().root_dir
        document_reports = []
        for document in docs:
            report = extractor.extract(document)
            try:
                report = augment_statement_table_report_with_engine_candidates(
                    root=root,
                    document=document,
                    statement_table_report=report,
                )
            except Exception as exc:
                report["warnings"] = [
                    *list(report.get("warnings") or []),
                    f"engine_augmentation_failed: {exc}",
                ]
            document_reports.append(report)
    report = build_extraction_report(ticker, period_from, period_to, reporting_standard, document_reports)
    save_extraction_report(report)
    return report


def _usable_statement_role(source_role: str | None) -> bool:
    role = source_role or ""
    return role == "financial_statements" or role.endswith("_with_embedded_financial_statements")


def _dedupe_documents(documents: list[ReportDocument]) -> list[ReportDocument]:
    by_key: dict[tuple[str, str | None, str | None], ReportDocument] = {}
    for doc in documents:
        key = (doc.report_period, doc.source_url, doc.file_hash)
        existing = by_key.get(key)
        if existing is None or doc.id > existing.id:
            by_key[key] = doc
    return sorted(by_key.values(), key=lambda item: (item.report_period, item.id))


def build_extraction_report(
    ticker: str,
    period_from: str,
    period_to: str,
    reporting_standard: str,
    document_reports: list[dict[str, Any]],
) -> dict[str, Any]:
    statement_tables = [table for report in document_reports for table in report.get("statement_tables", [])]
    coverage = statement_coverage(statement_tables)
    missing = required_statement_tables_missing(coverage)
    return {
        "company": ticker,
        "period_from": period_from,
        "period_to": period_to,
        "reporting_standard": reporting_standard.upper(),
        "validation_mode": "replay_cache",
        "generated_at": datetime.now(UTC).isoformat(),
        "documents_processed": len(document_reports),
        "tables_extracted": sum(int(report.get("tables_extracted") or 0) for report in document_reports),
        "statement_tables_count": sum(int(report.get("statement_tables_count") or 0) for report in document_reports),
        "balance_sheet_tables_count": sum(1 for table in statement_tables if table.get("statement_type") == "balance_sheet"),
        "income_statement_tables_count": sum(
            1 for table in statement_tables if table.get("statement_type") == "income_statement"
        ),
        "cash_flow_tables_count": sum(1 for table in statement_tables if table.get("statement_type") == "cash_flow"),
        "changes_in_equity_tables_count": sum(
            1 for table in statement_tables if table.get("statement_type") == "changes_in_equity"
        ),
        "statement_coverage": coverage,
        "required_statement_tables_found": not missing,
        "required_statement_tables_missing": missing,
        "facts_extracted": 0,
        "fact_parser_status": "not_invoked",
        "document_reports": document_reports,
        "artifact_paths": [report.get("artifact_path") for report in document_reports if report.get("artifact_path")],
        "warnings": sorted({warning for report in document_reports for warning in report.get("warnings", [])}),
    }


def save_extraction_report(report: dict[str, Any]) -> Path:
    root = get_settings().root_dir / "data" / "validation" / report["company"].upper()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{report['period_from']}_{report['period_to']}_statement_table_extraction.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract statement tables from cached/validated report documents.")
    parser.add_argument("ticker")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    parser.add_argument("--reporting-standard", default="IFRS", choices=["IFRS", "RAS"])
    parser.add_argument("--replay-cache", action="store_true")
    args = parser.parse_args(argv)
    report = extract_statement_tables(
        args.ticker,
        args.period_from,
        args.period_to,
        args.reporting_standard,
        replay_cache=args.replay_cache,
    )
    path = save_extraction_report(report)
    print(
        "\n".join(
            [
                f"company: {report['company']}",
                f"documents_processed: {report['documents_processed']}",
                f"tables_extracted: {report['tables_extracted']}",
                f"statement_tables_count: {report['statement_tables_count']}",
                f"balance_sheet_tables_count: {report['balance_sheet_tables_count']}",
                f"income_statement_tables_count: {report['income_statement_tables_count']}",
                f"cash_flow_tables_count: {report['cash_flow_tables_count']}",
                f"facts_extracted: {report['facts_extracted']}",
                f"fact_parser_status: {report['fact_parser_status']}",
                f"report_path: {path}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
