"""LLM narrative + recommendation for a stored PDF analysis.

Adapts the structured analytics (financial ratios extracted from the PDF plus
live market technical indicators) into the input shapes expected by the
project's own prompt templates (``app/services/llm/prompts.py``) and runs them
through :class:`LLMAnalysisService` — i.e. the fundamental + technical +
consolidated-summary stages that produce the BUY/HOLD/SELL recommendation.

Generated on demand and cached inside ``AnalysisResult.result_json['llm_report']``
so re-opening an analysis from history does not spend tokens again.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import get_settings
from app.db.models import AnalysisResult, Company, ReportDocument
from app.services.llm.analysis_service import LLMAnalysisService
from app.services.llm.client import LLMClient


class PdfInsightError(RuntimeError):
    pass


def _artifact_relative_path(path: str | Path | None) -> str | None:
    if not path:
        return None
    raw = Path(str(path))
    root = get_settings().root_dir.resolve()
    try:
        resolved = raw.resolve()
    except OSError:
        resolved = raw
    if resolved.is_absolute():
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError:
            return resolved.as_posix()
    return raw.as_posix()


def _normalize_llm_report_paths(report: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(report)
    normalized["summary_json_path"] = _artifact_relative_path(normalized.get("summary_json_path"))
    normalized["memo_markdown_path"] = _artifact_relative_path(normalized.get("memo_markdown_path"))
    return normalized


def generate_pdf_insight(db: Session, result_id: str, regenerate: bool = False) -> dict[str, Any]:
    result = db.get(AnalysisResult, result_id)
    if not result:
        raise PdfInsightError("Analysis not found")
    analytics = result.result_json or {}
    cached = analytics.get("llm_report")
    if cached and not regenerate:
        normalized_cached = _normalize_llm_report_paths(cached)
        if normalized_cached != cached:
            analytics["llm_report"] = normalized_cached
            result.result_json = analytics
            flag_modified(result, "result_json")
            db.commit()
        return normalized_cached

    settings = get_settings()
    client = LLMClient(api_key=settings.anthropic_api_key, model=settings.llm_model)
    if not client.available:
        raise PdfInsightError("ANTHROPIC_API_KEY не настроен — LLM недоступен")

    company = _company_payload(analytics)
    period = {
        "from": analytics.get("period_from"),
        "to": analytics.get("period_to"),
        "periods": analytics.get("periods") or [],
        "reporting_standard": analytics.get("reporting_standard", "IFRS"),
    }
    financial_metrics = _financial_metrics(analytics)
    structured_facts = list(analytics.get("structured_facts") or [])
    derived_safe_facts = list(analytics.get("derived_safe_facts") or [])
    analysis_readiness_summary = dict(analytics.get("analysis_readiness_summary") or {})
    top_blockers = list(analytics.get("top_blockers") or analytics.get("blockers") or [])
    unresolved_evidence_summary = _unresolved_evidence_summary(analytics)
    parser_risk_summary = _parser_risk_summary(analytics)
    market_analysis = _market_analysis(db, result)
    warnings = list(analytics.get("warnings") or [])
    data_quality = {
        "calculated_ratios": sum(1 for m in financial_metrics if m.get("quality_flag") == "exact"),
        "missing_ratios": sum(1 for m in financial_metrics if m.get("quality_flag") == "missing"),
        "extracted_facts": analytics.get("facts_count", 0),
        "structured_facts_count": len(structured_facts),
        "derived_safe_facts_count": len(derived_safe_facts),
        "rejected_rows_count": len(analytics.get("rejected_rows") or []),
        "unmapped_numeric_evidence_count": len(analytics.get("unmapped_numeric_evidence") or []),
        "unmapped_table_evidence_count": len(analytics.get("unmapped_table_evidence") or []),
        "document_validation_status": analytics.get("document_validation_status"),
        "periods_available": analytics.get("periods") or [],
        "market_data_available": bool(market_analysis.get("technical_indicators")),
        "analysis_readiness_summary": analysis_readiness_summary,
        "top_blockers": top_blockers,
    }
    source_documents = _source_documents(db, result, analytics)
    llm_input_context = {
        "company": company,
        "period": period,
        "financial_metrics": financial_metrics,
        "structured_facts": structured_facts,
        "derived_safe_facts": derived_safe_facts,
        "analysis_readiness_summary": analysis_readiness_summary,
        "top_blockers": top_blockers,
        "unresolved_evidence_summary": unresolved_evidence_summary,
        "parser_risk_summary": parser_risk_summary,
        "market_analysis": market_analysis,
        "warnings": warnings,
        "data_quality": data_quality,
        "source_documents": source_documents,
    }

    report = LLMAnalysisService(client).generate_full_report(
        company=company,
        period=period,
        financial_metrics=financial_metrics,
        structured_facts=structured_facts,
        derived_safe_facts=derived_safe_facts,
        analysis_readiness_summary=analysis_readiness_summary,
        top_blockers=top_blockers,
        unresolved_evidence_summary=unresolved_evidence_summary,
        parser_risk_summary=parser_risk_summary,
        market_analysis=market_analysis,
        peer_analysis={"peer_table": []},  # peers not computed in the PDF flow
        source_documents=source_documents,
        data_quality=data_quality,
        warnings=warnings,
    )

    summary_path, memo_path = _save_llm_artifacts(
        ticker=str(company.get("ticker") or "unknown"),
        period_from=str(period.get("from") or "unknown"),
        period_to=str(period.get("to") or "unknown"),
        structured_summary=report.structured_summary,
        memo_markdown=report.memo_markdown or report.full_markdown or report.overall_summary or "",
    )

    recommendation = report.recommendation if report.recommendation not in (None, "N/A") else None
    llm_report = {
        "text": report.full_markdown or report.overall_summary or report.technical_note or "",
        "recommendation": recommendation,
        "model": report.llm_model or settings.llm_model,
        "llm_model": report.llm_model or settings.llm_model,
        "fundamental_note": report.fundamental_note,
        "technical_note": report.technical_note,
        "peer_note": report.peer_note,
        "overall_summary": report.overall_summary,
        "structured_summary": report.structured_summary,
        "summary_json_path": _artifact_relative_path(summary_path),
        "memo_markdown_path": _artifact_relative_path(memo_path),
        "sections": {
            "fundamental": report.fundamental_note,
            "technical": report.technical_note,
            "overall": report.overall_summary,
        },
        "token_usage": report.token_usage,
        "market_included": bool(market_analysis.get("technical_indicators")),
        "warnings": list(report.warnings or []),
        "prompt_source": "app/services/llm/prompts.py",
        "input_context": llm_input_context,
    }
    analytics["llm_report"] = llm_report
    result.llm_payload_json = llm_input_context
    result.result_json = analytics
    result.report_markdown = report.memo_markdown or report.full_markdown or report.overall_summary or ""
    flag_modified(result, "llm_payload_json")
    flag_modified(result, "result_json")
    db.commit()
    return _normalize_llm_report_paths(llm_report)


# ----- adapters: PDF analytics -> prompt input shapes -------------------

def _company_payload(analytics: dict[str, Any]) -> dict[str, Any]:
    company = analytics.get("company") or {}
    name = company.get("name") or company.get("ticker")
    return {
        "ticker": company.get("ticker"),
        "name": name,
        "short_name": name,
        "sector": company.get("sector"),
        "reporting_standard": analytics.get("reporting_standard", "IFRS"),
    }


def _financial_metrics(analytics: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str | None, str | None]] = set()
    for m in analytics.get("ratios") or []:
        calculated = m.get("status") == "calculated"
        item = {
            "entry_type": "ratio",
            "metric_code": m.get("metric_code"),
            "metric_name": m.get("metric_name"),
            "period": m.get("period"),
            "value": m.get("value"),
            "display_value": m.get("display_value"),
            "formula": m.get("formula"),
            "quality_flag": "exact" if calculated else "missing",
            "status": m.get("status"),
        }
        key = (item["entry_type"], item.get("metric_code"), item.get("period"))
        if key not in seen:
            out.append(item)
            seen.add(key)
    for fact in analytics.get("structured_facts") or []:
        item = {
            "entry_type": "structured_fact",
            "metric_code": fact.get("metric_code"),
            "metric_name": fact.get("metric_name_original") or fact.get("metric_code"),
            "period": fact.get("period"),
            "value": fact.get("value"),
            "display_value": fact.get("raw_value"),
            "statement_type": fact.get("statement_type"),
            "quality_flag": fact.get("quality_flag") or "exact",
            "status": "confirmed_fact",
        }
        key = (item["entry_type"], item.get("metric_code"), item.get("period"))
        if key not in seen:
            out.append(item)
            seen.add(key)
    return out


def _source_documents(db: Session, result: AnalysisResult, analytics: dict[str, Any]) -> dict[str, Any]:
    documents = [
        {"id": d.get("id"), "file_name": d.get("file_name"), "period": d.get("period")}
        for d in (analytics.get("documents") or [])
    ]
    attachments: list[dict[str, Any]] = []
    document_ids = [d.get("id") for d in (analytics.get("documents") or []) if d.get("id")]
    if document_ids:
        rows = db.scalars(select(ReportDocument).where(ReportDocument.id.in_(document_ids))).all()
        for row in rows:
            path = str(row.storage_path or "").strip()
            if path and path.lower().endswith(".pdf"):
                attachments.append(
                    {
                        "path": path,
                        "source_document_id": row.id,
                        "file_name": row.file_name,
                        "period": row.report_period,
                    }
                )
    return {
        "documents": documents,
        "source_pdf_attachments": attachments,
        "manual_upload": {"source_pdf": attachments[0]} if attachments else {},
        "analysis_result_id": result.id,
    }


def _unresolved_evidence_summary(analytics: dict[str, Any]) -> dict[str, Any]:
    rejected_rows = list(analytics.get("rejected_rows") or [])
    unmapped_numeric = list(analytics.get("unmapped_numeric_evidence") or [])
    unmapped_tables = list(analytics.get("unmapped_table_evidence") or [])

    def labels(items: list[dict[str, Any]], key: str = "raw_label", limit: int = 12) -> list[str]:
        values: list[str] = []
        for item in items:
            value = str(item.get(key) or item.get("table_title") or "").strip()
            if value and value not in values:
                values.append(value)
            if len(values) >= limit:
                break
        return values

    return {
        "rejected_rows_count": len(rejected_rows),
        "unmapped_numeric_evidence_count": len(unmapped_numeric),
        "unmapped_table_evidence_count": len(unmapped_tables),
        "rejected_row_labels": labels(rejected_rows),
        "unmapped_numeric_labels": labels(unmapped_numeric),
        "unmapped_table_titles": labels(unmapped_tables, key="table_title"),
        "evidence_warnings": list((analytics.get("llm_ready_evidence_pack") or {}).get("evidence_warnings") or []),
    }


def _parser_risk_summary(analytics: dict[str, Any]) -> dict[str, Any]:
    suspicious_facts: list[dict[str, Any]] = []
    note_like_count = 0
    text_fallback_count = 0
    for fact in list(analytics.get("structured_facts") or []):
        raw_value = str(fact.get("raw_value") or "").strip()
        warnings = list(fact.get("warnings") or [])
        reasons: list[str] = []

        if "text_table_fallback_used" in warnings:
            text_fallback_count += 1
        if _looks_like_note_reference(raw_value):
            reasons.append("raw_value_looks_like_note_reference")
            note_like_count += 1
        if _looks_like_note_prefixed_amount(raw_value):
            reasons.append("raw_value_looks_like_note_prefixed_amount")
        if "lower_trust_text_fallback_statement_row" in warnings:
            reasons.append("lower_trust_text_fallback_statement_row")

        if not reasons:
            continue

        suspicious_facts.append(
            {
                "metric_code": fact.get("metric_code"),
                "metric_name_original": fact.get("metric_name_original"),
                "statement_type": fact.get("statement_type"),
                "period": fact.get("period"),
                "raw_label": fact.get("raw_label"),
                "raw_value": raw_value,
                "value": fact.get("value"),
                "source_page": fact.get("source_page") or fact.get("page_number"),
                "source_engine": fact.get("source_engine"),
                "reasons": reasons,
            }
        )

    return {
        "structured_facts_count": len(list(analytics.get("structured_facts") or [])),
        "suspicious_fact_count": len(suspicious_facts),
        "note_like_raw_value_count": note_like_count,
        "text_fallback_fact_count": text_fallback_count,
        "suspicious_facts": suspicious_facts[:20],
        "risk_summary": (
            "Review suspicious structured facts against the source PDF before relying on them."
            if suspicious_facts
            else "No obvious parser-side note-reference contamination detected in structured facts."
        ),
    }


def _looks_like_note_reference(raw_value: str) -> bool:
    compact = raw_value.replace(" ", "")
    return compact.isdigit() and 0 < len(compact) <= 2


def _looks_like_note_prefixed_amount(raw_value: str) -> bool:
    return bool(re.match(r"^\d{1,2}\s+\d{1,3}(?:[,\s]\d{3})+(?:\.\d+)?$", raw_value))


def _market_analysis(db: Session, result: AnalysisResult) -> dict[str, Any]:
    """Best-effort live market indicators for the company ticker."""
    try:
        from app.services.market.market_audit import run_market_technical_report

        company = db.get(Company, result.company_id)
        if not company:
            return {}
        board = str(company.board or "TQBR").strip().upper()
        if not board or board == "PENDING":
            return {}
        year = int(str(result.period_to)[:4])
        report = run_market_technical_report(
            ticker=company.ticker,
            period_from=f"{year - 1}Q1",
            period_to=result.period_to,
            board=board,
            mode="live",
            cache_root=get_settings().root_dir / "data" / "market_cache",
        )
        if (report.get("summary") or {}).get("candles_count", 0) <= 0:
            return {}
        return {
            "status": report.get("status"),
            "summary": report.get("summary"),
            "actual_candle_date_range": report.get("actual_candle_date_range"),
            "latest_summary": report.get("latest_summary"),
            "technical_indicators": report.get("technical_indicators"),
            "liquidity_metrics": report.get("liquidity_metrics"),
        }
    except Exception:
        return {}


def _save_llm_artifacts(
    *,
    ticker: str,
    period_from: str,
    period_to: str,
    structured_summary: dict[str, Any],
    memo_markdown: str,
) -> tuple[Path | None, Path | None]:
    root = get_settings().root_dir / "data" / "validation" / ticker.upper()
    root.mkdir(parents=True, exist_ok=True)
    base_name = f"{period_from}_{period_to}_llm"

    summary_path: Path | None = None
    memo_path: Path | None = None

    if structured_summary:
        summary_path = root / f"{base_name}_summary.json"
        summary_path.write_text(
            json.dumps(structured_summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if memo_markdown.strip():
        memo_path = root / f"{base_name}_memo.md"
        memo_path.write_text(memo_markdown, encoding="utf-8")

    return summary_path, memo_path
