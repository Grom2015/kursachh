import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.init_db import init_db
from app.db.models import AnalysisJob, AnalysisResult, Company, ReportDocument, StatementFact
from app.db.session import SessionLocal
from app.services.analysis.orchestrator import AnalysisOrchestrator
from app.services.analysis.result_builder import ResultBuilder
from app.services.market.market_audit import market_analysis_from_audit, run_market_audit
from app.services.metrics.metric_audit import audit_metric_rows, metric_status_counts
from app.services.metrics.metric_engine import MetricEngine
from app.services.parsing.lkoh_ifrs_pdf_parser import LKOHIFRSPDFParser
from app.services.parsing.reconciliation import SourceReconciler
from app.services.periods import periods_between
from app.services.reports.real_manifest import RealSourceManifest
from app.services.validation.lkoh_manual_verification import manual_verification_summary

KEY_FACTS = [
    "revenue",
    "ebitda",
    "operating_profit",
    "net_income",
    "total_assets",
    "total_equity",
    "total_debt",
    "cash_and_equivalents",
    "current_assets",
    "current_liabilities",
    "operating_cash_flow",
    "capex",
]

KEY_METRICS = [
    "revenue_growth",
    "ebitda_margin",
    "operating_margin",
    "net_margin",
    "roe",
    "roa",
    "debt_to_equity",
    "net_debt_to_ebitda",
    "current_ratio",
    "fcf",
    "fcf_margin",
    "pe_ratio",
    "ev_to_ebitda",
    "dividend_yield",
]

SOURCE_ROLES = ["press_release", "financial_statements", "financial_supplement", "annual_report", "other"]
PARSER_VERSION = "LKOHIFRSPDFParser:stage1.11b"


def run_validation(period_from: str, period_to: str, replay_cache: bool = False) -> dict[str, Any]:
    init_db()
    with SessionLocal() as db:
        company = db.scalar(select(Company).where(Company.ticker == "LKOH"))
        if not company:
            raise RuntimeError("LKOH is not seeded")
        if replay_cache:
            report = run_replay_cache_validation(db, company, period_from, period_to)
            path = save_validation_report(report)
            print_validation_summary(report, path)
            return report
        job = AnalysisJob(
            company_query="LKOH",
            period_from=period_from,
            period_to=period_to,
            reporting_standard="IFRS",
            include_market_data=True,
            include_peers=False,
            include_news=False,
            data_mode="real",
        )
        db.add(job)
        db.commit()
        AnalysisOrchestrator(db).run(job.id)
        db.refresh(job)
        result = db.get(AnalysisResult, job.result_id) if job.result_id else None
        report = build_validation_report(db, company, period_from, period_to, job, result, validation_mode="live")
        path = save_validation_report(report)
        print_validation_summary(report, path)
        return report


def run_replay_cache_validation(db: Session, company: Company, period_from: str, period_to: str) -> dict[str, Any]:
    documents = cached_documents_from_manifest(db, company, period_from, period_to)
    if not documents:
        report = empty_replay_fail_report(company, period_from, period_to)
        return report
    job = AnalysisJob(
        company_id=company.id,
        company_query="LKOH",
        period_from=period_from,
        period_to=period_to,
        reporting_standard="IFRS",
        include_market_data=False,
        include_peers=False,
        include_news=False,
        data_mode="real",
        status="running",
        stage="replay_cache_parsing",
        progress=0.25,
    )
    db.add(job)
    db.flush()
    warnings: list[str] = []
    parser = LKOHIFRSPDFParser()
    facts = []
    for document in documents:
        parsed = parser.parse(document)
        facts.extend(parsed)
        document.status = "parsed"
        warnings.extend(parser.warnings)
    reconciler = SourceReconciler()
    facts = reconciler.reconcile(facts)
    warnings.extend(reconciler.warnings)
    db.add_all(facts)
    db.commit()
    periods = periods_between(period_from, period_to)
    metrics = MetricEngine(db).calculate(
        company.id,
        periods,
        report_document_ids=[document.id for document in documents],
        exclude_fixture=True,
    )
    market_report = run_market_audit(period_from, period_to, live=False)
    market_analysis = market_analysis_from_audit(market_report)
    warnings.extend(market_analysis.get("warnings", []))
    result_json = ResultBuilder().build(
        company,
        period_from,
        period_to,
        "IFRS",
        facts,
        metrics,
        documents,
        market_analysis,
        {"peer_table": [], "peer_summary": {}, "warnings": []},
        warnings,
        data_mode="real",
    )
    result = AnalysisResult(
        job_id=job.id,
        company_id=company.id,
        period_from=period_from,
        period_to=period_to,
        reporting_standard="IFRS",
        data_snapshot_json={
            "data_mode": "real",
            "validation_mode": "replay_cache",
            "input_documents_source": "cached_raw_files",
            "real_data_used": True,
            "fixture_data_used": False,
        },
        result_json=result_json,
        llm_payload_json={},
        warnings_json=warnings,
        disclaimer=get_settings().disclaimer,
    )
    db.add(result)
    db.flush()
    job.result_id = result.id
    job.status = "succeeded"
    job.stage = "completed"
    job.progress = 1.0
    db.commit()
    report = build_validation_report(
        db,
        company,
        period_from,
        period_to,
        job=None,
        result=result,
        validation_mode="replay_cache",
        input_documents_source="cached_raw_files",
        document_ids_filter={document.id for document in documents},
    )
    return report


def cached_documents_from_manifest(
    db: Session, company: Company, period_from: str, period_to: str
) -> list[ReportDocument]:
    reports = RealSourceManifest().load_for_company(company.ticker, period_from, period_to, "IFRS")
    documents = []
    for report in reports:
        item = report.to_manifest_item()
        path = find_cached_raw_file(company.ticker, item)
        if not path:
            continue
        file_bytes = path.read_bytes()
        import hashlib

        doc = ReportDocument(
            company_id=company.id,
            report_period=item["period"],
            reporting_standard=item.get("reporting_standard", "IFRS"),
            document_type=item.get("document_type", "other"),
            source_role=item.get("source_role", item.get("document_type", "other")),
            source_type=item.get("source_type", "issuer_ir_manifest"),
            source_url=item.get("source_url"),
            storage_path=str(path),
            file_name=path.name,
            file_hash=hashlib.sha256(file_bytes).hexdigest(),
            language=item.get("language"),
            status="downloaded",
        )
        db.add(doc)
        db.flush()
        documents.append(doc)
    db.commit()
    return documents


def find_cached_raw_file(ticker: str, item: dict[str, Any]) -> Path | None:
    raw_root = (get_settings().root_dir / get_settings().report_download_dir).resolve()
    root = raw_root / ticker.upper() / item.get("reporting_standard", "IFRS") / item["period"]
    if not root.exists():
        return None
    parsed = urlparse(item.get("source_url") or "")
    basename = unquote(Path(parsed.path).name)
    candidates = [path for path in root.iterdir() if path.is_file()]
    if basename:
        matched = [path for path in candidates if path.name.endswith(basename)]
        if matched:
            candidate = max(matched, key=lambda path: (path.stat().st_mtime, path.stat().st_size))
            return candidate if candidate.resolve().is_relative_to(raw_root) else None
    if item.get("expected_file_type") == "pdf":
        pdfs = [path for path in candidates if path.suffix.casefold() == ".pdf"]
        if pdfs:
            candidate = max(pdfs, key=lambda path: (path.stat().st_mtime, path.stat().st_size))
            return candidate if candidate.resolve().is_relative_to(raw_root) else None
    candidate = max(candidates, key=lambda path: (path.stat().st_mtime, path.stat().st_size), default=None)
    if candidate and candidate.resolve().is_relative_to(raw_root):
        return candidate
    return None


def empty_replay_fail_report(company: Company, period_from: str, period_to: str) -> dict[str, Any]:
    summary = {
        "source_document_count": 0,
        "downloaded_document_count": 0,
        "parsed_document_count": 0,
        "facts_extracted_count": 0,
        "facts_from_financial_statements": 0,
        "facts_from_press_release": 0,
        "canonical_facts_count": 0,
        "high_confidence_fact_count": 0,
        "low_confidence_fact_count": 0,
        "missing_key_fact_count": len(periods_between(period_from, period_to)) * len(KEY_FACTS),
        "conflicting_fact_count": 0,
        "derived_fact_count": 0,
        "ytd_facts_count": 0,
        "derived_quarter_facts_count": 0,
        "balance_sheet_snapshot_facts_count": 0,
        "metrics_calculated_count": 0,
        "missing_metric_count": len(periods_between(period_from, period_to)) * len(KEY_METRICS),
        "overall_quality": "unavailable",
        "metrics_valid_count": 0,
        "metrics_questionable_count": 0,
        "metrics_invalid_count": 0,
        "metrics_missing_count": len(periods_between(period_from, period_to)) * len(KEY_METRICS),
    }
    return {
        "company": company.ticker,
        "period_from": period_from,
        "period_to": period_to,
        "reporting_standard": "IFRS",
        "data_mode": "real",
        "fixture_data_used": False,
        "validation_mode": "replay_cache",
        "input_documents_source": "cached_raw_files",
        "parser_version": PARSER_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "stale_data_warning": "Cached real documents not found; run live validation first.",
        "status": "FAIL",
        "summary": summary,
        "source_role_summary": build_source_role_summary([]),
        "parser_diagnostics": {
            "documents_with_tables": 0,
            "documents_without_tables": 0,
            "candidate_rows_count": 0,
            "strong_matches_count": 0,
            "weak_matches_count": 0,
        },
        "documents": [],
        "fact_coverage": build_fact_coverage(periods_between(period_from, period_to), []),
        "metric_coverage": build_metric_coverage(periods_between(period_from, period_to), []),
        "manual_review_sample": [],
        "manual_verification": {
            "golden_checks_total": 0,
            "verified_checks_count": 0,
            "passed_count": 0,
            "failed_count": 0,
            "pending_count": 0,
            "accuracy": None,
            "status": "not_started",
        },
        "warnings": ["Cached real documents not found; run live validation first."],
        "next_actions": ["Run live validation to download official LKOH real documents."],
    }


def build_validation_report(
    db: Session,
    company: Company,
    period_from: str,
    period_to: str,
    job: AnalysisJob | None = None,
    result: AnalysisResult | None = None,
    validation_mode: str = "live",
    input_documents_source: str = "downloaded_now",
    stale_data_warning: str | None = None,
    document_ids_filter: set[int] | None = None,
) -> dict[str, Any]:
    periods = periods_between(period_from, period_to)
    documents = [
        doc
        for doc in db.scalars(select(ReportDocument).where(ReportDocument.company_id == company.id)).all()
        if doc.report_period in periods
        and doc.source_type != "fixture"
        and (document_ids_filter is None or doc.id in document_ids_filter)
    ]
    document_ids = {document.id for document in documents}
    facts = [
        fact
        for fact in db.scalars(select(StatementFact).where(StatementFact.company_id == company.id)).all()
        if fact.period in periods
        and fact.report_document_id in document_ids
        and (fact.source_location or {}).get("source_type") != "fixture"
    ]
    result_json = result.result_json if result else {}
    metrics = result_json.get("financial_analysis", {}).get("metrics", [])
    fact_coverage = build_fact_coverage(periods, facts)
    metric_coverage = build_metric_coverage(periods, metrics)
    audits = load_parse_audits(documents)
    table_debugs = load_table_debugs(documents)
    audit_warnings = [warning for audit in audits for warning in audit.get("warnings", [])]
    warnings = sorted(set((job.warnings_json if job else []) + result_json.get("warnings", []) + audit_warnings))
    audited_metrics = audit_metric_rows(metric_coverage, facts)
    summary = build_summary(documents, facts, fact_coverage, metric_coverage, result_json, audited_metrics)
    source_role_summary = build_source_role_summary(documents)
    parser_diagnostics = build_parser_diagnostics(documents, table_debugs)
    validation_warnings = validation_rule_warnings(documents, audits, source_role_summary)
    manual_summary = manual_verification_summary(period_from, period_to, facts=facts)
    if manual_summary["status"] == "not_started":
        validation_warnings.append("Manual golden verification has not been performed.")
    warnings = sorted(set(warnings + validation_warnings))
    status = determine_status(summary, documents, facts, result_json, warnings)
    return {
        "company": company.ticker,
        "period_from": period_from,
        "period_to": period_to,
        "reporting_standard": "IFRS",
        "data_mode": "real",
        "fixture_data_used": result_json.get("data_quality", {}).get("fixture_data_used", False),
        "validation_mode": validation_mode,
        "input_documents_source": input_documents_source,
        "parser_version": PARSER_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "stale_data_warning": stale_data_warning,
        "status": status,
        "summary": summary,
        "source_role_summary": source_role_summary,
        "parser_diagnostics": parser_diagnostics,
        "documents": [document_payload(doc) for doc in documents],
        "fact_coverage": fact_coverage,
        "metric_coverage": metric_coverage,
        "manual_review_sample": manual_review_sample(facts, audits),
        "manual_verification": manual_summary,
        "warnings": warnings,
        "next_actions": next_actions(status, summary),
    }


def build_fact_coverage(periods: list[str], facts: list[StatementFact]) -> list[dict[str, Any]]:
    by_key = {(fact.period, fact.metric_code): fact for fact in facts}
    rows = []
    for period in periods:
        for metric_code in KEY_FACTS:
            fact = by_key.get((period, metric_code))
            if not fact:
                rows.append(
                    {
                        "period": period,
                        "metric_code": metric_code,
                        "status": "missing",
                        "value": None,
                        "currency": None,
                    "unit_multiplier": None,
                    "period_type": None,
                        "quality_flag": "missing",
                        "confidence_score": None,
                        "source_document_id": None,
                        "source_url": None,
                        "source_location": None,
                        "warnings": [f"{metric_code} not extracted for {period}"],
                    }
                )
                continue
            source_location = fact.source_location or {}
            status = (
                "conflicting"
                if fact.quality_flag == "conflicting_sources"
                else "low_confidence"
                if fact.quality_flag == "low_confidence_parse"
                else "extracted"
            )
            rows.append(
                {
                    "period": period,
                    "metric_code": metric_code,
                    "status": status,
                    "value": fact.value,
                    "currency": fact.currency,
                    "unit_multiplier": fact.unit_multiplier,
                    "period_type": fact.period_type,
                    "quality_flag": fact.quality_flag,
                    "confidence_score": fact.confidence_score,
                    "source_document_id": fact.report_document_id,
                    "source_url": source_location.get("source_url"),
                    "source_location": format_source_location(source_location),
                    "raw_label": source_location.get("raw_label") or fact.metric_name_original,
                    "source_references": source_references(source_location),
                    "ifrs_concept_code": source_location.get("ifrs_concept_code"),
                    "statement_context": source_location.get("statement_context") or source_location.get("table_title"),
                    "period_column_detected": source_location.get("period_column_detected"),
                    "period_coverage": period_coverage_label(source_location),
                    "candidate_score_breakdown": source_location.get("candidate_score_breakdown"),
                    "applied_issuer_override": source_location.get("applied_issuer_override"),
                    "warnings": traceability_warnings(fact),
                }
            )
    return rows


def build_metric_coverage(periods: list[str], metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {(item["period"], item["metric_code"]): item for item in metrics}
    rows = []
    for period in periods:
        for metric_code in KEY_METRICS:
            metric = by_key.get((period, metric_code))
            if not metric:
                rows.append(
                    {
                        "period": period,
                        "metric_code": metric_code,
                        "status": "missing",
                        "value": None,
                        "quality_flag": "missing",
                        "inputs": {},
                        "formula": None,
                        "warnings": [f"{metric_code} not produced for {period}"],
                    }
                )
                continue
            quality = metric.get("quality_flag")
            rows.append(
                {
                    "period": period,
                    "metric_code": metric_code,
                    "status": "missing" if quality == "missing" else "derived" if quality == "derived" else "calculated",
                    "value": metric.get("value"),
                    "quality_flag": quality,
                    "inputs": metric.get("inputs") or {},
                    "formula": metric.get("formula"),
                    "warnings": metric.get("warnings") or [],
                }
            )
    return rows


def build_summary(
    documents: list[ReportDocument],
    facts: list[StatementFact],
    fact_coverage: list[dict[str, Any]],
    metric_coverage: list[dict[str, Any]],
    result_json: dict[str, Any],
    audited_metrics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    high_confidence = [
        fact
        for fact in facts
        if fact.quality_flag in {"exact", "derived"} and (fact.confidence_score is None or fact.confidence_score >= 0.7)
    ]
    low_confidence = [fact for fact in facts if fact.quality_flag == "low_confidence_parse" or (fact.confidence_score or 1) < 0.7]
    metric_counts = metric_status_counts(audited_metrics or [])
    return {
        "source_document_count": len(documents),
        "downloaded_document_count": sum(1 for doc in documents if doc.status in {"downloaded", "parsed"}),
        "parsed_document_count": sum(1 for doc in documents if doc.status == "parsed"),
        "facts_extracted_count": len(facts),
        "facts_from_financial_statements": sum(
            1 for fact in facts if (fact.source_location or {}).get("source_role") == "financial_statements"
        ),
        "facts_from_press_release": sum(
            1 for fact in facts if (fact.source_location or {}).get("source_role") == "press_release"
        ),
        "canonical_facts_count": len(facts),
        "high_confidence_fact_count": len(high_confidence),
        "low_confidence_fact_count": len(low_confidence),
        "missing_key_fact_count": sum(1 for row in fact_coverage if row["status"] == "missing"),
        "conflicting_fact_count": sum(1 for fact in facts if fact.quality_flag == "conflicting_sources"),
        "derived_fact_count": sum(1 for fact in facts if fact.quality_flag == "derived"),
        "ytd_facts_count": sum(1 for fact in facts if fact.period_type == "ytd"),
        "derived_quarter_facts_count": sum(
            1 for fact in facts if fact.period_type == "quarter" and fact.quality_flag == "derived"
        ),
        "balance_sheet_snapshot_facts_count": sum(1 for fact in facts if fact.period_type == "balance_sheet_snapshot"),
        "metrics_calculated_count": sum(1 for row in metric_coverage if row["status"] in {"calculated", "derived"}),
        "missing_metric_count": sum(1 for row in metric_coverage if row["quality_flag"] == "missing"),
        "overall_quality": result_json.get("data_quality", {}).get("overall_quality", "unavailable"),
        **metric_counts,
    }


def build_source_role_summary(documents: list[ReportDocument]) -> dict[str, int]:
    return {role: sum(1 for document in documents if document.source_role == role) for role in SOURCE_ROLES}


def build_parser_diagnostics(documents: list[ReportDocument], table_debugs: list[dict[str, Any]]) -> dict[str, int]:
    debug_by_id = {item.get("document_id"): item for item in table_debugs}
    documents_with_tables = 0
    documents_without_tables = 0
    candidate_rows_count = 0
    strong_matches_count = 0
    weak_matches_count = 0
    for document in documents:
        debug = debug_by_id.get(document.id)
        tables_found = int((debug or {}).get("tables_found") or 0)
        if tables_found:
            documents_with_tables += 1
        else:
            documents_without_tables += 1
        rows = (debug or {}).get("candidate_rows", [])
        candidate_rows_count += len(rows)
        strong_matches_count += sum(1 for row in rows if (row.get("confidence_score") or 0) >= 0.8)
        weak_matches_count += sum(1 for row in rows if (row.get("confidence_score") or 0) < 0.8)
    return {
        "documents_with_tables": documents_with_tables,
        "documents_without_tables": documents_without_tables,
        "candidate_rows_count": candidate_rows_count,
        "strong_matches_count": strong_matches_count,
        "weak_matches_count": weak_matches_count,
    }


def validation_rule_warnings(
    documents: list[ReportDocument], audits: list[dict[str, Any]], source_role_summary: dict[str, int]
) -> list[str]:
    warnings = []
    proper_sources = source_role_summary.get("financial_statements", 0) + source_role_summary.get("financial_supplement", 0)
    if documents and proper_sources == 0 and source_role_summary.get("press_release", 0) == len(documents):
        warnings.append("Only press release sources found; full financial statement extraction not validated.")
    pdf_audits = [audit for audit in audits if audit.get("parser", "").casefold().endswith("pdfparser")]
    if pdf_audits and all(int(audit.get("tables_found") or 0) == 0 for audit in pdf_audits):
        warnings.append("No tables extracted from downloaded documents.")
    return warnings


def determine_status(
    summary: dict[str, Any],
    documents: list[ReportDocument],
    facts: list[StatementFact],
    result_json: dict[str, Any],
    warnings: list[str],
) -> str:
    data_quality = result_json.get("data_quality", {})
    source_role_summary = result_json.get("data_quality", {}).get("source_role_summary", {})
    only_press_releases = documents and all(document.source_role == "press_release" for document in documents)
    if not documents or not facts or data_quality.get("fixture_data_used") or "critical" in " ".join(warnings).casefold():
        return "FAIL"
    if only_press_releases or (
        source_role_summary
        and source_role_summary.get("press_release") == summary["source_document_count"]
        and not source_role_summary.get("financial_statements")
        and not source_role_summary.get("financial_supplement")
    ):
        return "FAIL"
    if (
        summary["facts_extracted_count"] == 0
        or summary["canonical_facts_count"] == 0
        or summary["high_confidence_fact_count"] == 0
        or summary["facts_from_financial_statements"] == 0
    ):
        return "FAIL"
    if not all_traceable(facts):
        return "FAIL"
    if (
        data_quality.get("real_data_used")
        and summary["facts_from_financial_statements"]
        and summary["high_confidence_fact_count"]
    ):
        if (
            summary["missing_key_fact_count"] == 0
            and summary["low_confidence_fact_count"] == 0
            and summary["conflicting_fact_count"] == 0
        ):
            return "PASS"
        return "PARTIAL"
    if (
        data_quality.get("real_data_used")
        and summary["source_document_count"] >= 1
        and summary["facts_extracted_count"] > 0
        and summary["conflicting_fact_count"] == 0
    ):
        if summary["missing_key_fact_count"] == 0 and summary["low_confidence_fact_count"] == 0:
            return "PASS"
        return "PARTIAL"
    return "FAIL"


def document_payload(document: ReportDocument) -> dict[str, Any]:
    return {
        "id": document.id,
        "company": document.company.ticker if document.company else "LKOH",
        "period": document.report_period,
        "reporting_standard": document.reporting_standard,
        "document_type": document.document_type,
        "source_role": document.source_role,
        "source_type": document.source_type,
        "source_url": document.source_url,
        "file_name": document.file_name,
        "file_hash": document.file_hash,
        "status": document.status,
    }


def load_parse_audits(documents: list[ReportDocument]) -> list[dict[str, Any]]:
    from app.services.parsing.audit import audit_path

    audits = []
    for document in documents:
        path = audit_path(document)
        if path.exists():
            audits.append(json.loads(path.read_text(encoding="utf-8")))
    return audits


def load_table_debugs(documents: list[ReportDocument]) -> list[dict[str, Any]]:
    from app.services.parsing.audit import audit_path

    debugs = []
    for document in documents:
        path = audit_path(document).with_name(f"{document.id}_table_debug.json")
        if path.exists():
            debugs.append(json.loads(path.read_text(encoding="utf-8")))
    return debugs


def manual_review_sample(facts: list[StatementFact], audits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parser_by_document = {audit.get("document_id"): audit.get("parser") for audit in audits}
    sample = []
    for fact in facts[:20]:
        location = fact.source_location or {}
        sample.append(
            {
                "metric_code": fact.metric_code,
                "period": fact.period,
                "value": fact.value,
                "source_url": location.get("source_url"),
                "source_location": format_source_location(location),
                "parser": parser_by_document.get(fact.report_document_id),
                "confidence_score": fact.confidence_score,
                "quality_flag": fact.quality_flag,
                "manual_review_status": "pending",
            }
        )
    return sample


def all_traceable(facts: list[StatementFact]) -> bool:
    return all(
        (fact.source_location or {}).get("source_url")
        and format_source_location(fact.source_location or {})
        for fact in facts
    )


def traceability_warnings(fact: StatementFact) -> list[str]:
    warnings = []
    location = fact.source_location or {}
    if not location.get("source_url"):
        warnings.append("source_url missing")
    if not format_source_location(location):
        warnings.append("source_location missing")
    return warnings


def format_source_location(location: dict[str, Any]) -> str | None:
    if not location:
        return None
    parts = []
    if location.get("page"):
        parts.append(f"page {location['page']}")
    if location.get("table"):
        parts.append(f"table {location['table']}")
    if location.get("line"):
        parts.append(f"line {location['line']}")
    if location.get("raw"):
        parts.append(str(location["raw"]))
    if not parts and location.get("formula"):
        parts.append(f"derived: {location['formula']}")
    return ", ".join(parts) if parts else None


def period_coverage_label(location: dict[str, Any]) -> str | None:
    if location.get("period_coverage"):
        return location.get("period_coverage")
    if location.get("ytd_months"):
        return f"{location['ytd_months']}M"
    if location.get("period_type") == "annual":
        return "FY"
    return location.get("period_type")


def source_references(location: dict[str, Any]) -> list[dict[str, Any]]:
    references = []
    for metric_code, payload in (location.get("inputs") or {}).items():
        source_location = payload.get("source_location") or {}
        references.append(
            {
                "metric_code": metric_code,
                "value": payload.get("value"),
                "source_url": source_location.get("source_url"),
                "document_id": source_location.get("document_id"),
                "period_type": source_location.get("period_type"),
                "source_location": format_source_location(source_location),
                "raw_label": source_location.get("raw_label"),
            }
        )
    return references


def next_actions(status: str, summary: dict[str, Any]) -> list[str]:
    actions = []
    if status == "FAIL":
        actions.append(
            "Fill data/manifests/lkoh_real_sources.yml with trusted LKOH IFRS report URLs "
            "or configure LKOH_IR_REPORTS_URL."
        )
    if summary["missing_key_fact_count"]:
        actions.append("Review missing key facts and add LKOH-specific parser mapping only where source rows are explicit.")
    if summary["low_confidence_fact_count"]:
        actions.append("Manually verify low-confidence facts against source pages/tables before using them analytically.")
    if summary["conflicting_fact_count"]:
        actions.append("Resolve conflicting source facts manually or add trusted source priority.")
    return actions


def save_validation_report(report: dict[str, Any]) -> Path:
    root = get_settings().root_dir / "data" / "validation" / "LKOH"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{report['period_from']}_{report['period_to']}_real_validation_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def print_validation_summary(report: dict[str, Any], path: Path) -> None:
    summary = report["summary"]
    lines = [
        f"company: {report['company']}",
        f"period: {report['period_from']}..{report['period_to']}",
        f"data_mode: {report['data_mode']}",
        f"source_document_count: {summary['source_document_count']}",
        f"documents_downloaded: {summary['downloaded_document_count']}",
        f"documents_parsed: {summary['parsed_document_count']}",
        f"facts_extracted: {summary['facts_extracted_count']}",
        f"facts_from_financial_statements: {summary['facts_from_financial_statements']}",
        f"facts_from_press_release: {summary['facts_from_press_release']}",
        f"canonical_facts_count: {summary['canonical_facts_count']}",
        f"high_confidence_facts: {summary['high_confidence_fact_count']}",
        f"low_confidence_facts: {summary['low_confidence_fact_count']}",
        f"missing_key_facts: {summary['missing_key_fact_count']}",
        f"conflicting_facts: {summary['conflicting_fact_count']}",
        f"derived_facts: {summary['derived_fact_count']}",
        f"ytd_facts: {summary['ytd_facts_count']}",
        f"derived_quarter_facts: {summary['derived_quarter_facts_count']}",
        f"balance_sheet_snapshot_facts: {summary['balance_sheet_snapshot_facts_count']}",
        f"metrics_calculated: {summary['metrics_calculated_count']}",
        f"metrics_valid: {summary.get('metrics_valid_count', 0)}",
        f"metrics_questionable: {summary.get('metrics_questionable_count', 0)}",
        f"metrics_invalid: {summary.get('metrics_invalid_count', 0)}",
        f"missing_metrics: {summary['missing_metric_count']}",
        f"overall_quality: {summary['overall_quality']}",
        f"final_status: {report['status']}",
        f"report_path: {path}",
    ]
    print("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    replay_cache = "--replay-cache" in argv
    argv = [item for item in argv if item != "--replay-cache"]
    if len(argv) != 2:
        print("Usage: python -m app.tools.validate_lkoh_real_extraction 2021Q1 2021Q4 [--replay-cache]")
        return 2
    run_validation(argv[0], argv[1], replay_cache=replay_cache)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
