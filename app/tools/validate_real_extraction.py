import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.db.init_db import init_db
from app.db.models import AnalysisJob, AnalysisResult, Company, ReportDocument, StatementFact
from app.db.session import SessionLocal
from app.services.analysis.result_builder import ResultBuilder
from app.services.market.market_audit import market_analysis_from_audit, run_market_audit
from app.services.metrics.metric_audit import audit_metric_rows, metric_status_counts
from app.services.metrics.metric_engine import MetricEngine
from app.services.parsing.gazp_ifrs_pdf_parser import GAZPIFRSPDFParser
from app.services.parsing.lkoh_ifrs_pdf_parser import LKOHIFRSPDFParser
from app.services.parsing.reconciliation import SourceReconciler
from app.services.parsing.tatn_ifrs_pdf_parser import TATNIFRSPDFParser
from app.services.periods import periods_between
from app.services.quality.auto_support_status import evaluate_auto_support_status
from app.services.reports.document_validator import DocumentValidator
from app.services.reports.downloader import ReportDownloader
from app.services.reports.real_manifest import RealSourceManifest
from app.tools.validate_lkoh_real_extraction import (
    KEY_FACTS,
    KEY_METRICS,
    build_fact_coverage,
    build_metric_coverage,
    build_parser_diagnostics,
    build_source_role_summary,
    cached_documents_from_manifest,
    document_payload,
    load_parse_audits,
    load_table_debugs,
    manual_review_sample,
)
from app.tools.verify_golden_facts import verify as verify_golden_facts


def parser_for_ticker(ticker: str):
    if ticker.upper() == "TATN":
        return TATNIFRSPDFParser()
    if ticker.upper() == "GAZP":
        return GAZPIFRSPDFParser()
    return LKOHIFRSPDFParser()


def run_validation(ticker: str, period_from: str, period_to: str, mode: str) -> dict[str, Any]:
    init_db()
    ticker = ticker.upper()
    with SessionLocal() as db:
        company = db.scalar(select(Company).where(Company.ticker == ticker))
        if not company:
            raise RuntimeError(f"{ticker} is not seeded")
        warnings: list[str] = []
        documents = (
            cached_documents_from_manifest(db, company, period_from, period_to)
            if mode == "replay_cache"
            else download_manifest_documents(db, company, period_from, period_to, warnings)
        )
        if not documents:
            report = empty_report(ticker, period_from, period_to, mode, "No real documents found or downloaded.", warnings)
            save_report(report)
            print_summary(report)
            return report
        parser = parser_for_ticker(ticker)
        facts: list[StatementFact] = []
        for document in documents:
            if not parser.can_parse(document):
                warnings.append(f"No issuer parser for document {document.id}")
                continue
            parsed = parser.parse(document)
            facts.extend(parsed)
            warnings.extend(parser.warnings)
            document.status = "parsed"
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
        market_report = run_market_audit(period_from, period_to, ticker=ticker, mode="replay-cache")
        market_analysis = market_analysis_from_audit(market_report)
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
            warnings + market_analysis.get("warnings", []),
            data_mode="real",
        )
        job = AnalysisJob(
            company_id=company.id,
            company_query=ticker,
            period_from=period_from,
            period_to=period_to,
            reporting_standard="IFRS",
            data_mode="real",
            status="succeeded",
            stage="completed",
            progress=1.0,
        )
        db.add(job)
        db.flush()
        result = AnalysisResult(
            job_id=job.id,
            company_id=company.id,
            period_from=period_from,
            period_to=period_to,
            reporting_standard="IFRS",
            data_snapshot_json={"data_mode": "real", "validation_mode": mode, "fixture_data_used": False},
            result_json=result_json,
            llm_payload_json={},
            warnings_json=warnings,
            disclaimer=get_settings().disclaimer,
        )
        db.add(result)
        db.flush()
        job.result_id = result.id
        db.commit()
        report = build_report(ticker, period_from, period_to, mode, documents, facts, result_json, warnings)
        auto_status = report["automated_support_status"]
        result_json["data_quality"].update(
            {
                "automated_support_status": auto_status["status"],
                "manual_action_required": auto_status["manual_action_required"],
                "automated_quality_score": auto_status["automated_quality_score"],
                "blockers": auto_status["blockers"],
                "warnings": auto_status["warnings"],
                "supported_output_scopes": auto_status.get("supported_output_scopes", []),
                "unsupported_output_scopes": auto_status.get("unsupported_output_scopes", []),
                "scope_statuses": auto_status.get("scope_statuses", {}),
                "manual_verification": {
                    "available": bool(report.get("manual_verification", {}).get("review_pack_total")),
                    "status": report.get("manual_verification", {}).get("status"),
                    "note": "QA metadata only",
                },
            }
        )
        result_json.setdefault("warnings", [])
        result.result_json = result_json
        db.commit()
        save_report(report)
        print_summary(report)
        return report


def download_manifest_documents(
    db, company: Company, period_from: str, period_to: str, warnings: list[str]
) -> list[ReportDocument]:
    reports = RealSourceManifest().load_for_company(company.ticker, period_from, period_to, "IFRS")
    downloader = ReportDownloader(db)
    documents = []
    for report in reports:
        try:
            documents.append(downloader.download(company, report.to_manifest_item()))
        except Exception as exc:
            warnings.append(str(exc))
            continue
    db.commit()
    return documents


def build_report(
    ticker: str,
    period_from: str,
    period_to: str,
    mode: str,
    documents: list[ReportDocument],
    facts: list[StatementFact],
    result_json: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    periods = periods_between(period_from, period_to)
    metrics = result_json.get("financial_analysis", {}).get("metrics", [])
    fact_coverage = build_fact_coverage(periods, facts)
    metric_coverage = build_metric_coverage(periods, metrics)
    audited = audit_metric_rows(metric_coverage, facts)
    metric_counts = metric_status_counts(audited)
    audits = load_parse_audits(documents)
    table_debugs = load_table_debugs(documents)
    high_confidence = [
        fact
        for fact in facts
        if fact.quality_flag in {"exact", "derived"} and (fact.confidence_score is None or fact.confidence_score >= 0.7)
    ]
    summary = {
        "source_document_count": len(documents),
        "downloaded_document_count": sum(1 for doc in documents if doc.status in {"downloaded", "parsed"}),
        "parsed_document_count": sum(1 for doc in documents if doc.status == "parsed"),
        "facts_extracted_count": len(facts),
        "canonical_facts_count": len(facts),
        "high_confidence_fact_count": len(high_confidence),
        "missing_key_fact_count": sum(1 for row in fact_coverage if row["status"] == "missing"),
        "conflicting_fact_count": sum(1 for fact in facts if fact.quality_flag == "conflicting_sources"),
        "metrics_calculated_count": sum(1 for row in metric_coverage if row["status"] in {"calculated", "derived"}),
        "metrics_missing_count": sum(1 for row in metric_coverage if row["quality_flag"] == "missing"),
        **metric_counts,
    }
    source_package_status = automated_source_package_status(documents, periods)
    manual_summary = manual_verification(ticker, period_from, period_to)
    auto_support = evaluate_auto_support_status(
        company=ticker,
        period_from=period_from,
        period_to=period_to,
        reporting_standard="IFRS",
        source_package_status=source_package_status,
        documents=documents,
        facts=facts,
        metrics=audited,
        manual_verification=manual_summary,
        data_mode="real",
    ).to_dict()
    document_validations = document_validation_payload(documents)
    status = "FAIL"
    if documents and facts and summary["high_confidence_fact_count"] >= 10:
        status = "PARTIAL" if summary["missing_key_fact_count"] else "PASS"
    elif documents and facts:
        status = "PARTIAL"
    return {
        "company": ticker,
        "period_from": period_from,
        "period_to": period_to,
        "reporting_standard": "IFRS",
        "data_mode": "real",
        "validation_mode": mode,
        "generated_at": datetime.now(UTC).isoformat(),
        "fixture_data_used": False,
        "status": status,
        "summary": summary,
        "automated_support_status": auto_support,
        "automated_quality_score": auto_support["automated_quality_score"],
        "source_quality_score": auto_support["source_quality_score"],
        "parser_quality_score": auto_support["parser_quality_score"],
        "metric_quality_score": auto_support["metric_quality_score"],
        "consistency_check_summary": auto_support["consistency_check_summary"],
        "manual_action_required": False,
        "source_role_summary": build_source_role_summary(documents),
        "parser_diagnostics": build_parser_diagnostics(documents, table_debugs),
        "conflict_breakdown": conflict_breakdown(facts),
        "manual_verification": manual_summary,
        "documents": [document_payload(document) for document in documents],
        "document_validations": document_validations,
        "fact_coverage": fact_coverage,
        "metric_coverage": audited,
        "manual_review_sample": manual_review_sample(facts, audits),
        "warnings": sorted(set(warnings)),
        "next_actions": next_actions(ticker, status, summary),
    }


def empty_report(
    ticker: str, period_from: str, period_to: str, mode: str, warning: str, extra_warnings: list[str] | None = None
) -> dict[str, Any]:
    periods = periods_between(period_from, period_to)
    return {
        "company": ticker,
        "period_from": period_from,
        "period_to": period_to,
        "reporting_standard": "IFRS",
        "data_mode": "real",
        "validation_mode": mode,
        "generated_at": datetime.now(UTC).isoformat(),
        "fixture_data_used": False,
        "status": "FAIL",
        "summary": {
            "source_document_count": 0,
            "downloaded_document_count": 0,
            "parsed_document_count": 0,
            "facts_extracted_count": 0,
            "canonical_facts_count": 0,
            "high_confidence_fact_count": 0,
            "missing_key_fact_count": len(periods) * len(KEY_FACTS),
            "conflicting_fact_count": 0,
            "metrics_calculated_count": 0,
            "metrics_valid_count": 0,
            "metrics_questionable_count": 0,
            "metrics_invalid_count": 0,
            "metrics_missing_count": len(periods) * len(KEY_METRICS),
        },
        "automated_support_status": {
            "company": ticker,
            "period_from": period_from,
            "period_to": period_to,
            "reporting_standard": "IFRS",
            "status": "SOURCE_BLOCKED",
            "source_package_status": "NOT_READY",
            "document_verification_status": "blocked",
            "parser_status": "parser_blocked",
            "fact_coverage_status": "missing",
            "metric_coverage_status": "missing",
            "automated_quality_score": 0.0,
            "manual_action_required": False,
            "blockers": ["No verified/cached real documents available."],
            "warnings": [],
            "supported_outputs": {
                "financial_facts": False,
                "financial_metrics": False,
                "market_analysis": True,
                "peer_comparison": False,
                "llm_payload": False,
            },
            "supported_output_scopes": ["market_technical_analysis"],
            "unsupported_output_scopes": [
                "statement_based_financials",
                "valuation_metrics",
                "peer_comparison",
                "llm_summary",
            ],
            "scope_statuses": {
                "statement_based_financials": {
                    "status": "UNSUPPORTED",
                    "score": 0.0,
                    "eligible_metrics_count": 0,
                    "calculated_eligible_metrics_count": 0,
                    "questionable_eligible_metrics_count": 0,
                    "missing_expected_metrics_count": 0,
                    "unsupported_metrics_count": 0,
                },
                "valuation_metrics": {
                    "status": "UNAVAILABLE",
                    "reason": "market_cap / EV / dividends missing",
                },
                "market_technical_analysis": {"status": "AUTO_READY", "score": 1.0},
                "peer_comparison": {"status": "UNSUPPORTED"},
                "llm_summary": {"status": "AUTO_PARTIAL"},
            },
            "source_quality_score": 0.0,
            "parser_quality_score": 0.0,
            "metric_quality_score": 0.0,
            "consistency_check_summary": {
                "passed_checks": [],
                "failed_checks": [],
                "warnings": [],
                "quality_penalties": 0.0,
            },
        },
        "automated_quality_score": 0.0,
        "source_quality_score": 0.0,
        "parser_quality_score": 0.0,
        "metric_quality_score": 0.0,
        "consistency_check_summary": {
            "passed_checks": [],
            "failed_checks": [],
            "warnings": [],
            "quality_penalties": 0.0,
        },
        "manual_action_required": False,
        "parser_diagnostics": {
            "documents_with_tables": 0,
            "documents_without_tables": 0,
            "candidate_rows_count": 0,
            "strong_matches_count": 0,
            "weak_matches_count": 0,
        },
        "documents": [],
        "document_validations": [],
        "conflict_breakdown": [],
        "manual_verification": manual_verification(ticker, period_from, period_to),
        "fact_coverage": [],
        "metric_coverage": [],
        "manual_review_sample": [],
        "warnings": sorted(set([warning] + list(extra_warnings or []))),
        "next_actions": [f"Add/download trusted {ticker} real financial statements first."],
    }


def conflict_breakdown(facts: list[StatementFact]) -> list[dict[str, Any]]:
    rows = []
    for fact in facts:
        if fact.quality_flag != "conflicting_sources":
            continue
        location = fact.source_location or {}
        candidates = location.get("conflicting_values") or []
        source_locations = [candidate.get("source_location") for candidate in candidates]
        raw_labels = [
            (candidate.get("source_location") or {}).get("raw_label")
            for candidate in candidates
            if candidate.get("source_location")
        ]
        reason = likely_conflict_reason(candidates)
        rows.append(
            {
                "period": fact.period,
                "metric_code": fact.metric_code,
                "period_type": fact.period_type,
                "candidate_values": [candidate.get("value") for candidate in candidates],
                "source_locations": source_locations,
                "raw_labels": raw_labels,
                "likely_reason": reason,
                "recommended_action": conflict_recommendation(reason),
            }
        )
    return rows


def manual_verification(ticker: str, period_from: str, period_to: str) -> dict[str, Any]:
    report = verify_golden_facts(ticker, period_from, period_to)
    status = "not_started"
    if report.get("pending_count"):
        status = "in_progress"
    if report.get("status") == "PASS":
        status = "pass"
    if report.get("status") == "FAIL":
        status = "fail"
    return {
        "status": status,
        "review_pack_total": report.get("review_pack_total", 0),
        "review_pack_verified": report.get("review_pack_verified", 0),
        "review_pack_accuracy": report.get("review_pack_accuracy"),
        "pending_count": report.get("pending_count", 0),
        "verified_checks_count": report.get("verified_checks_count", 0),
        "passed_count": report.get("passed_count", 0),
        "failed_count": report.get("failed_count", 0),
        "accuracy": report.get("accuracy"),
    }


def likely_conflict_reason(candidates: list[dict[str, Any]]) -> str:
    values = [candidate.get("value") for candidate in candidates]
    labels = [
        ((candidate.get("source_location") or {}).get("raw_label") or "").casefold()
        for candidate in candidates
    ]
    tables = [
        ((candidate.get("source_location") or {}).get("table_title") or "").casefold()
        for candidate in candidates
    ]
    if values and all(value == values[0] for value in values):
        return "duplicate_match"
    if len(set(tables)) > 1:
        return "same_label_multiple_tables"
    if any("note" in table for table in tables) or len(set(labels)) == 1:
        return "wrong_column"
    return "unknown"


def conflict_recommendation(reason: str) -> str:
    recommendations = {
        "duplicate_match": "Merge duplicate same-value candidates and keep source references.",
        "different_period_coverage": "Keep facts separate by period_type/coverage instead of marking conflict.",
        "wrong_column": "Restrict parser context to primary statement rows or explicitly trusted note rows.",
        "same_label_multiple_tables": "Use table title/context to choose only comparable source rows.",
        "unit_mismatch": "Fix unit parsing before reconciliation.",
        "unknown": "Manual review required before selecting a canonical value.",
    }
    return recommendations[reason]


def automated_source_package_status(documents: list[ReportDocument], periods: list[str]) -> str:
    proper_roles = {"financial_statements", "financial_supplement", "annual_report"}
    periods_with_proper = {document.report_period for document in documents if document.source_role in proper_roles}
    if not periods_with_proper:
        return "NOT_READY"
    if all(period in periods_with_proper for period in periods):
        return "READY"
    return "PARTIAL"


def document_validation_payload(documents: list[ReportDocument]) -> list[dict[str, Any]]:
    validator = DocumentValidator()
    rows = []
    for document in documents:
        row = validator.validate(document).to_dict()
        row["document_id"] = document.id
        row["period"] = document.report_period
        row["source_url"] = document.source_url
        row["source_role"] = document.source_role
        rows.append(row)
    return rows


def next_actions(ticker: str, status: str, summary: dict[str, Any]) -> list[str]:
    actions = []
    if status == "FAIL":
        actions.append(f"Verify {ticker} source package and parser mappings before peer comparison.")
    if summary.get("missing_key_fact_count"):
        actions.append(f"Review missing {ticker} key facts; do not invent absent values.")
    return actions


def save_report(report: dict[str, Any]) -> Path:
    root = get_settings().root_dir / "data" / "validation" / report["company"].upper()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{report['period_from']}_{report['period_to']}_real_validation_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def print_summary(report: dict[str, Any]) -> None:
    path = get_settings().root_dir / "data" / "validation" / report["company"].upper() / (
        f"{report['period_from']}_{report['period_to']}_real_validation_report.json"
    )
    summary = report["summary"]
    print(
        "\n".join(
            [
                f"company: {report['company']}",
                f"status: {report['status']}",
                f"source_document_count: {summary['source_document_count']}",
                f"canonical_facts_count: {summary['canonical_facts_count']}",
                f"high_confidence_fact_count: {summary['high_confidence_fact_count']}",
                f"metrics_valid: {summary.get('metrics_valid_count', 0)}",
                f"metrics_questionable: {summary.get('metrics_questionable_count', 0)}",
                f"metrics_missing: {summary.get('metrics_missing_count', 0)}",
                f"report_path: {path}",
            ]
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate real extraction for a ticker.")
    parser.add_argument("ticker")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--replay-cache", action="store_true")
    args = parser.parse_args(argv)
    mode = "replay_cache" if args.replay_cache else "live" if args.live else "live"
    run_validation(args.ticker, args.period_from, args.period_to, mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
