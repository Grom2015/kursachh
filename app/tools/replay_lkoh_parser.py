import json
import sys
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.init_db import init_db
from app.db.models import Company, ReportDocument
from app.db.session import SessionLocal
from app.services.parsing.audit import audit_path
from app.services.parsing.lkoh_ifrs_pdf_parser import LKOHIFRSPDFParser
from app.tools.validate_lkoh_real_extraction import cached_documents_from_manifest


def run_parser_replay(period_from: str, period_to: str, db: Session | None = None) -> dict[str, Any]:
    if db is None:
        init_db()
        with SessionLocal() as session:
            return _run_parser_replay(session, period_from, period_to)
    return _run_parser_replay(db, period_from, period_to)


def _run_parser_replay(db: Session, period_from: str, period_to: str) -> dict[str, Any]:
    company = db.scalar(select(Company).where(Company.ticker == "LKOH"))
    if not company:
        return _empty_report(period_from, period_to, ["LKOH is not seeded"])

    documents = [
        document
        for document in cached_documents_from_manifest(db, company, period_from, period_to)
        if document.source_role == "financial_statements"
    ]
    if not documents:
        return _empty_report(period_from, period_to, ["Cached LKOH financial statement documents not found"])

    parser = LKOHIFRSPDFParser()
    warnings: list[str] = []
    extracted_facts_count = 0
    high_confidence_count = 0
    derived_fact_count = 0
    table_debugs: list[dict[str, Any]] = []

    for document in documents:
        facts = parser.parse(document)
        document.status = "parsed"
        warnings.extend(parser.warnings)
        extracted_facts_count += len(facts)
        high_confidence_count += sum(
            1
            for fact in facts
            if fact.quality_flag in {"exact", "derived"} and (fact.confidence_score is None or fact.confidence_score >= 0.7)
        )
        derived_fact_count += sum(1 for fact in facts if fact.quality_flag == "derived")
        table_debugs.append(_read_table_debug(document))
    db.commit()

    report = {
        "company": "LKOH",
        "period_from": period_from,
        "period_to": period_to,
        "documents_processed": len(documents),
        "tables_found": sum(int(item.get("tables_found") or 0) for item in table_debugs),
        "candidate_rows_count": sum(len(item.get("candidate_rows", [])) for item in table_debugs),
        "strong_matches_count": sum(
            1
            for item in table_debugs
            for row in item.get("candidate_rows", [])
            if (row.get("confidence_score") or 0) >= 0.8
        ),
        "weak_matches_count": sum(
            1
            for item in table_debugs
            for row in item.get("candidate_rows", [])
            if (row.get("confidence_score") or 0) < 0.8
        ),
        "extracted_facts_count": extracted_facts_count,
        "high_confidence_count": high_confidence_count,
        "derived_fact_count": derived_fact_count,
        "warnings": sorted(set(warnings)),
    }
    return report


def _empty_report(period_from: str, period_to: str, warnings: list[str]) -> dict[str, Any]:
    return {
        "company": "LKOH",
        "period_from": period_from,
        "period_to": period_to,
        "documents_processed": 0,
        "tables_found": 0,
        "candidate_rows_count": 0,
        "strong_matches_count": 0,
        "weak_matches_count": 0,
        "extracted_facts_count": 0,
        "high_confidence_count": 0,
        "derived_fact_count": 0,
        "warnings": warnings,
    }


def _read_table_debug(document: ReportDocument) -> dict[str, Any]:
    path = audit_path(document).with_name(f"{document.id}_table_debug.json")
    if not path.exists():
        return {"document_id": document.id, "tables_found": 0, "candidate_rows": [], "warnings": ["table debug missing"]}
    return json.loads(path.read_text(encoding="utf-8"))


def print_summary(report: dict[str, Any]) -> None:
    print(
        "\n".join(
            [
                f"company: {report['company']}",
                f"period: {report['period_from']}..{report['period_to']}",
                f"documents_processed: {report['documents_processed']}",
                f"tables_found: {report['tables_found']}",
                f"candidate_rows_count: {report['candidate_rows_count']}",
                f"strong_matches_count: {report['strong_matches_count']}",
                f"weak_matches_count: {report['weak_matches_count']}",
                f"extracted_facts_count: {report['extracted_facts_count']}",
                f"high_confidence_count: {report['high_confidence_count']}",
                f"derived_fact_count: {report['derived_fact_count']}",
            ]
        )
    )
    if report["warnings"]:
        print("warnings:")
        for warning in report["warnings"]:
            print(f"- {warning}")


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if len(argv) != 2:
        print("Usage: python -m app.tools.replay_lkoh_parser 2021Q1 2021Q4")
        return 2
    report = run_parser_replay(argv[0], argv[1])
    print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
