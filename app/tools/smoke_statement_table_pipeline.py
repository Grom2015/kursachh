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
from app.services.parsing.dataframe_loader import load_statement_table_as_dataframe
from app.services.periods import period_in_range
from app.services.reports.financial_report_discovery import (
    FinancialReportDiscoveryRequest,
    FinancialReportDiscoveryService,
)
from app.tools.extract_statement_tables import (
    _dedupe_documents,
    _usable_statement_role,
    extract_statement_tables,
)


def run_smoke(
    ticker: str,
    period_from: str,
    period_to: str,
    reporting_standard: str = "IFRS",
    replay_cache: bool = True,
) -> dict[str, Any]:
    if not replay_cache:
        raise RuntimeError("Statement table smoke supports replay-cache mode only.")
    init_db()
    ticker = ticker.upper()
    with SessionLocal() as db:
        company = db.scalar(select(Company).where(Company.ticker == ticker))
        if not company:
            raise RuntimeError(f"Company not found: {ticker}")
        discovery_service = FinancialReportDiscoveryService(db)
        discovery = discovery_service.discover(
            FinancialReportDiscoveryRequest(
                company_query=ticker,
                ticker=ticker,
                period_from=period_from,
                period_to=period_to,
                reporting_standard=reporting_standard,
                live=False,
            )
        )
        documents_available = _cached_documents_count(db, company, period_from, period_to, reporting_standard)
    extraction = extract_statement_tables(ticker, period_from, period_to, reporting_standard, replay_cache=True)
    dataframe_status = _load_required_dataframes(extraction)
    acceptance_minimum = (
        bool(discovery.discovered_reports)
        and documents_available > 0
        and dataframe_status["balance_sheet_dataframe_loaded"]
        and dataframe_status["income_statement_dataframe_loaded"]
        and extraction.get("facts_extracted") == 0
        and extraction.get("fact_parser_status") == "not_invoked"
    )
    warnings = []
    if not dataframe_status["cash_flow_dataframe_loaded"]:
        warnings.append("Cash flow DataFrame was not loaded; cash flow extraction is desirable but not required.")
    return {
        "company": ticker,
        "period_from": period_from,
        "period_to": period_to,
        "reporting_standard": reporting_standard.upper(),
        "generated_at": datetime.now(UTC).isoformat(),
        "discovery_status": discovery.status,
        "discovered_reports_count": len(discovery.discovered_reports),
        "documents_available": documents_available,
        "tables_extracted": extraction.get("tables_extracted", 0),
        "statement_tables_count": extraction.get("statement_tables_count", 0),
        "balance_sheet_tables": extraction.get("balance_sheet_tables_count", 0),
        "income_statement_tables": extraction.get("income_statement_tables_count", 0),
        "cash_flow_tables": extraction.get("cash_flow_tables_count", 0),
        "statement_coverage": extraction.get("statement_coverage", {}),
        "balance_sheet_dataframe_loaded": dataframe_status["balance_sheet_dataframe_loaded"],
        "income_statement_dataframe_loaded": dataframe_status["income_statement_dataframe_loaded"],
        "cash_flow_dataframe_loaded": dataframe_status["cash_flow_dataframe_loaded"],
        "dataframe_artifacts": dataframe_status["dataframe_artifacts"],
        "facts_extracted": extraction.get("facts_extracted", 0),
        "fact_parser_status": extraction.get("fact_parser_status", "not_invoked"),
        "task_acceptance_minimum_passed": acceptance_minimum,
        "warnings": sorted(set(warnings + extraction.get("warnings", []))),
    }


def save_smoke_report(report: dict[str, Any]) -> Path:
    root = get_settings().root_dir / "data" / "validation" / report["company"].upper()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{report['period_from']}_{report['period_to']}_statement_table_pipeline_smoke.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def _cached_documents_count(
    db,
    company: Company,
    period_from: str,
    period_to: str,
    reporting_standard: str,
) -> int:
    docs = db.scalars(
        select(ReportDocument).where(
            ReportDocument.company_id == company.id,
            ReportDocument.reporting_standard == reporting_standard.upper(),
            ReportDocument.source_type != "fixture",
            ReportDocument.status.in_(["downloaded", "parsed", "validated"]),
        )
    ).all()
    usable_docs = [
        doc
        for doc in docs
        if period_in_range(doc.report_period, period_from, period_to) and _usable_statement_role(doc.source_role)
    ]
    return len(_dedupe_documents(usable_docs))


def _load_required_dataframes(extraction: dict[str, Any]) -> dict[str, Any]:
    status = {
        "balance_sheet_dataframe_loaded": False,
        "income_statement_dataframe_loaded": False,
        "cash_flow_dataframe_loaded": False,
        "dataframe_artifacts": [],
    }
    for report in extraction.get("document_reports", []):
        document_id = report.get("document_id")
        if not document_id:
            continue
        artifact = report.get("artifact_path")
        for statement_type, key in [
            ("balance_sheet", "balance_sheet_dataframe_loaded"),
            ("income_statement", "income_statement_dataframe_loaded"),
            ("cash_flow", "cash_flow_dataframe_loaded"),
        ]:
            if status[key]:
                continue
            try:
                frame = load_statement_table_as_dataframe(int(document_id), statement_type)
            except FileNotFoundError:
                continue
            if not frame.empty and len(frame.columns) > 0:
                status[key] = True
                status["dataframe_artifacts"].append(
                    {"document_id": document_id, "statement_type": statement_type, "artifact_path": artifact}
                )
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test financial report discovery to statement table DataFrames.")
    parser.add_argument("ticker")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    parser.add_argument("--reporting-standard", default="IFRS", choices=["IFRS", "RAS"])
    parser.add_argument("--replay-cache", action="store_true")
    args = parser.parse_args(argv)
    report = run_smoke(
        args.ticker,
        args.period_from,
        args.period_to,
        args.reporting_standard,
        replay_cache=args.replay_cache,
    )
    path = save_smoke_report(report)
    print(
        "\n".join(
            [
                f"company: {report['company']}",
                f"discovery_status: {report['discovery_status']}",
                f"documents_available: {report['documents_available']}",
                f"tables_extracted: {report['tables_extracted']}",
                f"balance_sheet_dataframe_loaded: {report['balance_sheet_dataframe_loaded']}",
                f"income_statement_dataframe_loaded: {report['income_statement_dataframe_loaded']}",
                f"cash_flow_dataframe_loaded: {report['cash_flow_dataframe_loaded']}",
                f"task_acceptance_minimum_passed: {report['task_acceptance_minimum_passed']}",
                f"facts_extracted: {report['facts_extracted']}",
                f"fact_parser_status: {report['fact_parser_status']}",
                f"report_path: {path}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
