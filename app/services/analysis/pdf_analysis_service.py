"""PDF-driven analysis on top of the existing AnalysisResult history/versioning.

This service is the bridge between the manual PDF ingestion pipeline
(``ManualReportIngestionService`` -> ``DataFrameStatementParser``) and the
existing analysis history (``AnalysisJob`` / ``AnalysisResult``).

* ``analyze`` parses an uploaded PDF and stores the result as a version-1
  ``AnalysisResult`` so it shows up in the history.
* ``augment`` ("Дополнить анализ из другого PDF") ingests a second PDF without
  discarding the first, re-parses facts across *both* documents and stores a
  child ``AnalysisResult`` (``version + 1``, ``parent_result_id``).

Period handling: facts are always extracted with their natural (PDF-derived)
periods so nothing is dropped. When the user types an explicit period for a
document, that document's facts are *relabelled* onto the requested period
(shifting comparative columns by the same offset), so the report shows the
period the user asked for instead of one auto-detected from the PDF text.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import AnalysisJob, AnalysisResult, Company, ReportDocument
from app.services.metrics.financial_ratios_calculator import (
    FinancialRatiosCalculator,
    FinancialRatiosRequest,
)
from app.services.parsing.dataframe_statement_parser import DataFrameStatementParser
from app.services.periods import PERIOD_RE, period_key
from app.services.reports.manual_report_ingestion import (
    ManualReportIngestionRequest,
    ManualReportIngestionService,
)

# Wide range so artifact/table period filters never drop a document's tables;
# the real (tight) reporting periods are derived from the extracted facts.
WIDE_PERIOD_FROM = "1990Q1"
WIDE_PERIOD_TO = "2100Q4"


class PdfAnalysisError(ValueError):
    """Raised when a PDF cannot be turned into an analysis."""


def normalize_user_period(raw: str | None) -> str | None:
    """Return a canonical ``YYYYQn`` period from user input, or ``None``.

    Accepts ``2023`` (-> ``2023Q4``), ``2023q2`` (-> ``2023Q2``). Empty or
    unrecognised input returns ``None`` (i.e. "auto-detect from the PDF").
    """
    value = str(raw or "").strip().upper()
    if not value:
        return None
    if value.isdigit() and len(value) == 4:
        return f"{value}Q4"
    if PERIOD_RE.match(value):
        return value
    return None


def _coerce_period(period: str | None) -> str | None:
    value = str(period or "").strip().upper()
    if not value:
        return None
    if value.isdigit() and len(value) == 4:
        return f"{value}Q4"
    return value if PERIOD_RE.match(value) else None


def _period_index(period: str) -> int:
    year, quarter = period_key(period)
    return year * 4 + (quarter - 1)


def _period_from_index(index: int) -> str:
    year, quarter = divmod(index, 4)
    return f"{year}Q{quarter + 1}"


class PdfAnalysisService:
    def __init__(self, db: Session, root: Path | None = None):
        self.db = db
        self.root = (root or get_settings().root_dir).resolve()
        self.settings = get_settings()

    # ----- public API ---------------------------------------------------

    def analyze(
        self,
        *,
        local_file_path: str,
        company_ticker: str,
        period: str | None = None,
        reporting_standard: str = "IFRS",
        original_filename: str | None = None,
    ) -> AnalysisResult:
        """Parse a fresh PDF and store it as a new (version 1) analysis."""
        requested = normalize_user_period(period)
        report = self._ingest(
            local_file_path=local_file_path,
            company_ticker=company_ticker,
            period=requested or self._default_period(),
            reporting_standard=reporting_standard,
            purge_superseded=True,
        )
        company = self._require_company(report.company_ticker)
        new_id = report.report_document_id
        forced_periods = {new_id: requested} if (new_id and requested) else {}
        analytics = self._build_analytics(
            company=company,
            reporting_standard=reporting_standard,
            document_ids=[new_id] if new_id else [],
            forced_periods=forced_periods,
            ingest_report=report,
            uploaded_filenames=[original_filename or Path(local_file_path).name],
        )
        return self._store_result(
            company=company,
            reporting_standard=reporting_standard,
            analytics=analytics,
            document_ids=[new_id] if new_id else [],
            uploaded_filenames=analytics["documents_summary"]["uploaded_filenames"],
            parent=None,
        )

    def augment(
        self,
        *,
        parent_result_id: str,
        local_file_path: str,
        period: str | None = None,
        original_filename: str | None = None,
    ) -> AnalysisResult:
        """Add a second PDF to an existing analysis and store a new version."""
        parent = self.db.get(AnalysisResult, parent_result_id)
        if not parent:
            raise PdfAnalysisError("Analysis not found")
        company = parent.company
        reporting_standard = parent.reporting_standard
        snapshot = parent.data_snapshot_json or {}
        existing_ids = list(snapshot.get("document_ids") or [])
        existing_files = list(snapshot.get("uploaded_filenames") or [])
        # Per-document target periods already established for the existing docs.
        existing_periods: dict[int, str] = {
            int(k): v for k, v in (snapshot.get("document_periods") or {}).items() if v
        }

        requested = normalize_user_period(period)
        report = self._ingest(
            local_file_path=local_file_path,
            company_ticker=company.ticker,
            period=requested or self._default_period(),
            reporting_standard=reporting_standard,
            purge_superseded=False,
        )
        new_id = report.report_document_id
        combined_ids = list(dict.fromkeys([*existing_ids, *([new_id] if new_id else [])]))
        uploaded_filenames = [*existing_files, original_filename or Path(local_file_path).name]

        forced_periods: dict[int, str] = dict(existing_periods)
        if new_id and requested:
            forced_periods[new_id] = requested

        analytics = self._build_analytics(
            company=company,
            reporting_standard=reporting_standard,
            document_ids=combined_ids,
            forced_periods=forced_periods,
            ingest_report=report,
            uploaded_filenames=uploaded_filenames,
        )
        analytics["delta"] = self._build_delta(parent.result_json or {}, analytics)
        return self._store_result(
            company=company,
            reporting_standard=reporting_standard,
            analytics=analytics,
            document_ids=combined_ids,
            uploaded_filenames=uploaded_filenames,
            parent=parent,
        )

    # ----- ingestion + analytics ---------------------------------------

    def _ingest(
        self,
        *,
        local_file_path: str,
        company_ticker: str,
        period: str,
        reporting_standard: str,
        purge_superseded: bool,
    ):
        service = ManualReportIngestionService(self.db, root=self.root)
        report = service.ingest(
            ManualReportIngestionRequest(
                company_ticker=company_ticker,
                period=period,
                reporting_standard=reporting_standard,
                local_file_path=local_file_path,
                document_type="financial_statements",
                manual_upload_reason="user_requested",
                run_table_extraction=True,
                run_dataframe_fact_parser=True,
                allow_text_fallback_semantic_gate=True,
                persist_facts=False,
                auto_fetch_market_data=False,
                purge_superseded=purge_superseded,
            )
        )
        if not report.report_document_id:
            raise PdfAnalysisError(
                "Не удалось обработать файл: "
                + (report.blockers[0] if report.blockers else report.status)
            )
        return report

    def _build_analytics(
        self,
        *,
        company: Company,
        reporting_standard: str,
        document_ids: list[int],
        forced_periods: dict[int, str],
        ingest_report,
        uploaded_filenames: list[str],
    ) -> dict[str, Any]:
        # Extract facts across every document of the analysis with their natural
        # PDF-derived periods (wide range so nothing is filtered out).
        parse = DataFrameStatementParser(
            self.db,
            root=self.root,
            allow_text_fallback_semantic_gate=True,
        ).parse(
            company.ticker,
            WIDE_PERIOD_FROM,
            WIDE_PERIOD_TO,
            reporting_standard,
            document_ids=document_ids,
        )
        parse_payload = parse.to_dict()
        raw_facts = parse_payload.get("facts") or []

        # Resolve, per document, the target reporting period (explicit user input
        # wins; otherwise the document's own latest detected period) and relabel.
        document_periods = self._resolve_document_periods(raw_facts, document_ids, forced_periods)
        relabelled = self._relabel_facts(raw_facts, document_periods)
        structured_facts = self._relabel_periodized_items(
            parse_payload.get("structured_facts") or [],
            document_periods,
        )
        derived_safe_facts = self._relabel_periodized_items(
            parse_payload.get("derived_safe_facts") or [],
            document_periods,
        )
        rejected_rows = self._relabel_periodized_items(
            parse_payload.get("rejected_rows") or [],
            document_periods,
        )
        unmapped_numeric_evidence = self._relabel_periodized_items(
            parse_payload.get("unmapped_numeric_evidence") or [],
            document_periods,
        )
        unmapped_table_evidence = self._relabel_periodized_items(
            parse_payload.get("unmapped_table_evidence") or [],
            document_periods,
        )
        llm_ready_evidence_pack = self._relabel_periodized_items(
            parse_payload.get("llm_ready_evidence_pack") or {},
            document_periods,
        )
        analysis_readiness_summary = self._relabel_periodized_items(
            parse_payload.get("analysis_readiness_summary") or {},
            document_periods,
        )

        periods = sorted({f["period"] for f in relabelled if f.get("period")}, key=period_key)
        if periods:
            period_from, period_to = periods[0], periods[-1]
        else:
            fallback = next(iter(document_periods.values()), None) or self._default_period()
            period_from = period_to = fallback

        # Persist the relabelled fact set where the ratios calculator reads it.
        self._save_parse_report(company.ticker, period_from, period_to, relabelled, reporting_standard)
        ratios = FinancialRatiosCalculator(self.db, root=self.root).calculate(
            FinancialRatiosRequest(
                company_ticker=company.ticker,
                period_from=period_from,
                period_to=period_to,
                reporting_standard=reporting_standard,
                source="dataframe_parse_report",
                allow_text_fallback_candidates=True,
            )
        )
        ratios_payload = ratios.to_dict()

        # Reflect the resolved period back onto the stored documents.
        self._apply_document_periods(document_periods)

        facts = self._facts_view(relabelled)
        calculated = [m for m in ratios_payload.get("metrics", []) if m.get("status") == "calculated"]
        documents = self._documents_view(document_ids, uploaded_filenames, document_periods)
        warnings = sorted(set(
            list(ingest_report.warnings or [])
            + list(parse.warnings or [])
            + list(ratios_payload.get("warnings") or [])
        ))
        return {
            "kind": "pdf_analysis",
            "company": {
                "ticker": company.ticker,
                "name": company.short_name or company.full_name or company.ticker,
                "sector": company.sector,
            },
            "reporting_standard": reporting_standard,
            "period_from": period_from,
            "period_to": period_to,
            "periods": periods,
            "document_periods": {str(k): v for k, v in document_periods.items()},
            "documents": documents,
            "documents_summary": {
                "count": len(documents),
                "uploaded_filenames": uploaded_filenames,
            },
            "ratios": ratios_payload.get("metrics", []),
            "ratios_summary": {
                **ratios_payload.get("summary", {}),
                "calculated_metrics": [
                    {
                        "metric_code": m["metric_code"],
                        "metric_name": m["metric_name"],
                        "period": m["period"],
                        "value": m["value"],
                        "display_value": m["display_value"],
                        "formula": m.get("formula"),
                    }
                    for m in calculated
                ],
            },
            "facts": facts,
            "facts_count": len(facts),
            "structured_facts": structured_facts,
            "structured_facts_count": len(structured_facts),
            "derived_safe_facts": derived_safe_facts,
            "derived_safe_facts_count": len(derived_safe_facts),
            "rejected_rows": rejected_rows,
            "rejected_rows_count": len(rejected_rows),
            "unmapped_numeric_evidence": unmapped_numeric_evidence,
            "unmapped_numeric_evidence_count": len(unmapped_numeric_evidence),
            "unmapped_table_evidence": unmapped_table_evidence,
            "unmapped_table_evidence_count": len(unmapped_table_evidence),
            "analysis_readiness_summary": analysis_readiness_summary,
            "llm_ready_evidence_pack": llm_ready_evidence_pack,
            "statement_coverage": ingest_report.statement_coverage or {},
            "fact_parse_status": ingest_report.fact_parse_status,
            "document_validation_status": ingest_report.document_validation_status,
            "warnings": warnings,
            "blockers": list(ingest_report.blockers or []),
            "top_blockers": list(
                dict.fromkeys(
                    [
                        *(ingest_report.blockers or []),
                        *(
                            (analysis_readiness_summary.get("blocked_areas") or [])
                            if isinstance(analysis_readiness_summary, dict)
                            else []
                        ),
                    ]
                )
            ),
        }

    # ----- period relabelling ------------------------------------------

    def _resolve_document_periods(
        self,
        raw_facts: list[dict[str, Any]],
        document_ids: list[int],
        forced_periods: dict[int, str],
    ) -> dict[int, str]:
        """Map each document id -> its target latest reporting period."""
        natural: dict[int, int] = {}
        for fact in raw_facts:
            doc_id = fact.get("source_document_id")
            coerced = _coerce_period(fact.get("period"))
            if doc_id is None or coerced is None:
                continue
            idx = _period_index(coerced)
            if doc_id not in natural or idx > natural[doc_id]:
                natural[doc_id] = idx

        resolved: dict[int, str] = {}
        for doc_id in document_ids:
            forced = forced_periods.get(doc_id)
            if forced:
                resolved[doc_id] = forced
            elif doc_id in natural:
                resolved[doc_id] = _period_from_index(natural[doc_id])
        return resolved

    def _relabel_facts(
        self,
        raw_facts: list[dict[str, Any]],
        document_periods: dict[int, str],
    ) -> list[dict[str, Any]]:
        # Per document, build a mapping of its natural (PDF-derived) periods onto
        # reporting periods anchored at the document's target. The newest column
        # becomes the target; each older column becomes the immediately preceding
        # reporting period at the report's own frequency — the convention
        # financial statements actually use:
        #   * interim (target Qn, n in 1..3) -> previous quarter
        #       2026Q3 -> 2026Q2, 2026Q1 -> 2025Q4
        #   * annual / full year (target Q4) -> previous fiscal year-end
        #       2024Q4 -> 2023Q4
        distinct_by_doc: dict[int, set[str]] = {}
        for fact in raw_facts:
            doc_id = fact.get("source_document_id")
            coerced = _coerce_period(fact.get("period"))
            if doc_id is None or coerced is None:
                continue
            distinct_by_doc.setdefault(doc_id, set()).add(coerced)

        mapping_by_doc: dict[int, dict[str, str]] = {}
        for doc_id, target in document_periods.items():
            periods = sorted(distinct_by_doc.get(doc_id, set()), key=_period_index)
            if not periods:
                continue
            target_year, target_quarter = period_key(target)
            annual = target_quarter == 4
            target_index = _period_index(target)
            mapping: dict[str, str] = {}
            for rank, natural in enumerate(reversed(periods)):
                if rank == 0:
                    mapping[natural] = target
                elif annual:
                    mapping[natural] = f"{target_year - rank}Q4"
                else:
                    mapping[natural] = _period_from_index(target_index - rank)
            mapping_by_doc[doc_id] = mapping

        out: list[dict[str, Any]] = []
        for fact in raw_facts:
            item = dict(fact)
            doc_id = fact.get("source_document_id")
            coerced = _coerce_period(fact.get("period"))
            mapping = mapping_by_doc.get(doc_id, {})
            if coerced is not None:
                item["period"] = mapping.get(coerced, coerced)
            out.append(item)
        return out

    def _relabel_periodized_items(
        self,
        payload: Any,
        document_periods: dict[int, str],
    ) -> Any:
        if not document_periods:
            return payload

        mapping_by_doc = self._period_mapping_by_document(document_periods, payload)
        return self._relabel_periodized_value(payload, mapping_by_doc)

    def _period_mapping_by_document(
        self,
        document_periods: dict[int, str],
        payload: Any,
    ) -> dict[int, dict[str, str]]:
        discovered: dict[int, set[str]] = {}

        def visit(node: Any, doc_id: int | None = None) -> None:
            if isinstance(node, dict):
                local_doc_id = (
                    node.get("source_document_id")
                    or node.get("report_document_id")
                    or node.get("document_id")
                    or doc_id
                )
                for key, value in node.items():
                    if key == "period" and local_doc_id is not None:
                        coerced = _coerce_period(value)
                        if coerced:
                            discovered.setdefault(int(local_doc_id), set()).add(coerced)
                    elif key == "period_context" and isinstance(value, dict):
                        for nested_key, nested_value in value.items():
                            if nested_key in {"period", "current_period", "comparative_period"} and local_doc_id is not None:
                                coerced = _coerce_period(nested_value)
                                if coerced:
                                    discovered.setdefault(int(local_doc_id), set()).add(coerced)
                            elif nested_key == "periods" and isinstance(nested_value, list) and local_doc_id is not None:
                                for period in nested_value:
                                    coerced = _coerce_period(period)
                                    if coerced:
                                        discovered.setdefault(int(local_doc_id), set()).add(coerced)
                            else:
                                visit(nested_value, local_doc_id)
                    else:
                        visit(value, local_doc_id)
            elif isinstance(node, list):
                for item in node:
                    visit(item, doc_id)

        visit(payload)

        mapping_by_doc: dict[int, dict[str, str]] = {}
        for raw_doc_id, target in document_periods.items():
            doc_id = int(raw_doc_id)
            periods = sorted(discovered.get(doc_id, set()), key=_period_index)
            if not periods:
                continue
            target_year, target_quarter = period_key(target)
            annual = target_quarter == 4
            target_index = _period_index(target)
            mapping: dict[str, str] = {}
            for rank, natural in enumerate(reversed(periods)):
                if rank == 0:
                    mapping[natural] = target
                elif annual:
                    mapping[natural] = f"{target_year - rank}Q4"
                else:
                    mapping[natural] = _period_from_index(target_index - rank)
            mapping_by_doc[doc_id] = mapping
        return mapping_by_doc

    def _relabel_periodized_value(
        self,
        node: Any,
        mapping_by_doc: dict[int, dict[str, str]],
        doc_id: int | None = None,
    ) -> Any:
        if isinstance(node, dict):
            local_doc_id = node.get("source_document_id") or node.get("report_document_id") or node.get("document_id") or doc_id
            result: dict[str, Any] = {}
            for key, value in node.items():
                if key == "period" and local_doc_id is not None:
                    result[key] = self._mapped_period(int(local_doc_id), value, mapping_by_doc)
                elif key == "period_context" and isinstance(value, dict):
                    period_context: dict[str, Any] = {}
                    for nested_key, nested_value in value.items():
                        if nested_key in {"period", "current_period", "comparative_period"} and local_doc_id is not None:
                            period_context[nested_key] = self._mapped_period(
                                int(local_doc_id),
                                nested_value,
                                mapping_by_doc,
                            )
                        elif nested_key == "periods" and isinstance(nested_value, list) and local_doc_id is not None:
                            period_context[nested_key] = [
                                self._mapped_period(int(local_doc_id), item, mapping_by_doc)
                                for item in nested_value
                            ]
                        else:
                            period_context[nested_key] = self._relabel_periodized_value(
                                nested_value,
                                mapping_by_doc,
                                int(local_doc_id) if local_doc_id is not None else None,
                            )
                    result[key] = period_context
                else:
                    result[key] = self._relabel_periodized_value(
                        value,
                        mapping_by_doc,
                        int(local_doc_id) if local_doc_id is not None else None,
                    )
            return result
        if isinstance(node, list):
            return [self._relabel_periodized_value(item, mapping_by_doc, doc_id) for item in node]
        return node

    def _mapped_period(
        self,
        doc_id: int,
        value: Any,
        mapping_by_doc: dict[int, dict[str, str]],
    ) -> Any:
        coerced = _coerce_period(value)
        if coerced is None:
            return value
        return mapping_by_doc.get(doc_id, {}).get(coerced, coerced)

    def _apply_document_periods(self, document_periods: dict[int, str]) -> None:
        if not document_periods:
            return
        rows = self.db.scalars(
            select(ReportDocument).where(ReportDocument.id.in_(list(document_periods)))
        ).all()
        for row in rows:
            target = document_periods.get(row.id)
            if target and row.report_period != target:
                row.report_period = target
        self.db.commit()

    def _save_parse_report(
        self,
        ticker: str,
        period_from: str,
        period_to: str,
        facts: list[dict[str, Any]],
        reporting_standard: str,
    ) -> None:
        root = self.root / "data" / "validation" / ticker.upper()
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{period_from}_{period_to}_dataframe_statement_fact_parse.json"
        payload = {
            "company_ticker": ticker.upper(),
            "period_from": period_from,
            "period_to": period_to,
            "reporting_standard": reporting_standard,
            "facts": facts,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # ----- persistence --------------------------------------------------

    def _store_result(
        self,
        *,
        company: Company,
        reporting_standard: str,
        analytics: dict[str, Any],
        document_ids: list[int],
        uploaded_filenames: list[str],
        parent: AnalysisResult | None,
    ) -> AnalysisResult:
        version = (parent.version + 1) if parent else 1
        job = AnalysisJob(
            company_id=company.id,
            company_query=company.ticker,
            period_from=analytics["period_from"],
            period_to=analytics["period_to"],
            reporting_standard=reporting_standard,
            include_market_data=False,
            include_peers=False,
            include_news=False,
            data_mode="manual_upload",
            status="succeeded",
            stage="completed",
            progress=1.0,
            result_id=None,
        )
        self.db.add(job)
        self.db.flush()

        result = AnalysisResult(
            job_id=job.id,
            parent_result_id=parent.id if parent else None,
            version=version,
            company_id=company.id,
            period_from=analytics["period_from"],
            period_to=analytics["period_to"],
            reporting_standard=reporting_standard,
            data_snapshot_json={
                "data_mode": "manual_upload",
                "document_ids": document_ids,
                "uploaded_filenames": uploaded_filenames,
                "document_periods": analytics.get("document_periods", {}),
                "source": "pdf_upload" if parent is None else "pdf_augment",
            },
            result_json=analytics,
            llm_payload_json={},
            warnings_json=analytics.get("warnings", []),
            disclaimer=self.settings.disclaimer,
        )
        self.db.add(result)
        self.db.flush()
        job.result_id = result.id
        self.db.commit()
        self.db.refresh(result)
        return result

    # ----- views / helpers ---------------------------------------------

    def _facts_view(self, facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        view: list[dict[str, Any]] = []
        for fact in facts:
            if fact.get("value") is None:
                continue
            view.append(
                {
                    "metric_code": fact.get("metric_code"),
                    "label": fact.get("raw_label") or fact.get("metric_code"),
                    "statement_type": fact.get("source_table_type"),
                    "period": fact.get("period"),
                    "value": fact.get("value"),
                    "currency": fact.get("currency"),
                    "quality_flag": fact.get("quality_flag"),
                }
            )
        view.sort(key=lambda f: (f.get("statement_type") or "", f.get("period") or "", f.get("metric_code") or ""))
        return view

    def _documents_view(
        self,
        document_ids: list[int],
        uploaded_filenames: list[str],
        document_periods: dict[int, str],
    ) -> list[dict[str, Any]]:
        rows = {
            r.id: r
            for r in self.db.scalars(select(ReportDocument).where(ReportDocument.id.in_(document_ids))).all()
        } if document_ids else {}
        documents: list[dict[str, Any]] = []
        for index, doc_id in enumerate(document_ids):
            doc = rows.get(doc_id)
            name = uploaded_filenames[index] if index < len(uploaded_filenames) else None
            documents.append(
                {
                    "id": doc_id,
                    "file_name": name or (doc.file_name if doc else None),
                    "period": document_periods.get(doc_id) or (doc.report_period if doc else None),
                    "status": doc.status if doc else None,
                    "sha256": doc.file_hash if doc else None,
                }
            )
        return documents

    def _build_delta(self, old_analytics: dict[str, Any], new_analytics: dict[str, Any]) -> dict[str, Any]:
        def index(analytics: dict[str, Any]) -> dict[tuple[str, str], Any]:
            return {
                (m["metric_code"], m["period"]): m
                for m in analytics.get("ratios", [])
                if m.get("status") == "calculated"
            }

        old = index(old_analytics)
        new = index(new_analytics)
        added = [v for k, v in new.items() if k not in old]
        changed = []
        for key, item in new.items():
            if key in old and old[key].get("value") != item.get("value"):
                changed.append(
                    {
                        "metric_code": key[0],
                        "period": key[1],
                        "old_value": old[key].get("value"),
                        "new_value": item.get("value"),
                    }
                )
        old_periods = set(old_analytics.get("periods") or [])
        new_periods = set(new_analytics.get("periods") or [])
        return {
            "added_periods": sorted(new_periods - old_periods, key=period_key),
            "added_metric_count": len(added),
            "changed_metrics": changed,
        }

    def _require_company(self, ticker: str) -> Company:
        company = self.db.scalar(select(Company).where(Company.ticker == ticker.upper()))
        if not company:
            raise PdfAnalysisError(f"Company not found after ingestion: {ticker}")
        return company

    def _default_period(self) -> str:
        return f"{datetime.now().year}Q4"


def history_items(db: Session, limit: int = 50) -> list[dict[str, Any]]:
    """Server-side history of PDF analyses (manual_upload jobs only)."""
    rows = db.scalars(
        select(AnalysisResult)
        .join(AnalysisJob, AnalysisResult.job_id == AnalysisJob.id)
        .where(AnalysisJob.data_mode == "manual_upload")
        .order_by(AnalysisResult.created_at.desc())
        .limit(limit)
    ).all()
    items: list[dict[str, Any]] = []
    for result in rows:
        snapshot = result.data_snapshot_json or {}
        analytics = result.result_json or {}
        items.append(
            {
                "id": result.id,
                "company": result.company.ticker,
                "company_name": (analytics.get("company") or {}).get("name"),
                "period_from": result.period_from,
                "period_to": result.period_to,
                "version": result.version,
                "parent_result_id": result.parent_result_id,
                "created_at": result.created_at.isoformat() if result.created_at else None,
                "document_count": len(snapshot.get("document_ids") or []),
                "calculated_count": (analytics.get("ratios_summary") or {}).get("calculated_count", 0),
                "facts_count": analytics.get("facts_count", 0),
            }
        )
    return items
