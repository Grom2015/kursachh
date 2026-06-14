from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.services.parsing.pdf_auto_parse_orchestrator import synthesize_engine_results

SCHEMA_VERSION = "1.0"
PIPELINE_VERSION = "canonical_machine_report_v1"
EXTRACTION_POLICY_VERSION = "native_extraction_v1"
CONFIDENCE_POLICY_VERSION = "confidence_policy_v1"
INTERPRETER_POLICY_VERSION = "interpreter_policy_v1"
EVIDENCE_RETENTION_SCOPE = (
    "V1 retains all numeric/table material actually extracted or touched by the available native extraction stack. "
    "It does not recover unreadable image-only content when OCR is unavailable."
)
DOCUMENT_TIMEOUT_SECONDS = 120
PAGE_OCR_TIMEOUT_SECONDS = 15
MAX_OCR_PAGES = 8


@dataclass
class MachineReportBuildResult:
    payload: dict[str, Any]
    path: Path


class CanonicalMachineReportBuilder:
    def __init__(self, root: Path | None = None):
        self.root = (root or get_settings().root_dir).resolve()

    def build_from_manual_ingestion(self, report: Any) -> MachineReportBuildResult:
        validation = dict(report.validation_result or {})
        parse_report = self._best_available_parse_report(report, dict(report.fact_parse_report or {}))
        statement_table_report = self._load_first_artifact(report.dataframe_artifacts or [])
        ocr_available = paddle_ocr_available()
        extraction_coverage = self._extraction_coverage(statement_table_report, ocr_available=ocr_available)
        extraction_coverage = self._merge_warning_based_coverage(
            extraction_coverage,
            table_warnings=list(statement_table_report.get("warnings") or []),
            report_warnings=list(report.warnings or []),
            ocr_available=ocr_available,
        )
        ocr_status = self._ocr_status(statement_table_report, extraction_coverage, ocr_available)
        engine_bundle = synthesize_engine_results(
            root=self.root,
            stored_document_path=report.stored_document_path,
            statement_table_report=statement_table_report,
            parse_report=parse_report,
            extraction_coverage=extraction_coverage,
        )
        ocr_status = self._ocr_status_from_engine_results(
            ocr_status,
            extraction_coverage,
            engine_bundle["engine_results"],
        )

        structured_facts = list(parse_report.get("structured_facts") or [])
        derived_safe_facts = list(parse_report.get("derived_safe_facts") or [])
        rejected_rows = list(parse_report.get("rejected_rows") or [])
        unmapped_numeric_evidence = list(parse_report.get("unmapped_numeric_evidence") or [])
        unmapped_table_evidence = list(parse_report.get("unmapped_table_evidence") or [])
        if not unmapped_table_evidence:
            unmapped_table_evidence = self._fallback_table_evidence(statement_table_report)

        ratios_summary = self._ratios_summary(report, parse_report)
        top_blockers = list(
            dict.fromkeys(
                [
                    *list(report.blockers or []),
                    *self._cached_structural_blockers(report, parse_report),
                    *self._ocr_blockers(ocr_status, extraction_coverage),
                    *self._parse_stage_blockers(parse_report),
                ]
            )
        )
        stage_results = self._stage_results(
            report=report,
            validation=validation,
            statement_table_report=statement_table_report,
            parse_report=parse_report,
            ratios_summary=ratios_summary,
            ocr_status=ocr_status,
            extraction_coverage=extraction_coverage,
            top_blockers=top_blockers,
        )
        analysis_readiness = self._analysis_readiness_summary(
            report=report,
            parse_report=parse_report,
            structured_facts=structured_facts,
            derived_safe_facts=derived_safe_facts,
            rejected_rows=rejected_rows,
            unmapped_numeric_evidence=unmapped_numeric_evidence,
            unmapped_table_evidence=unmapped_table_evidence,
            top_blockers=top_blockers,
            ocr_status=ocr_status,
            extraction_coverage=extraction_coverage,
        )
        final_status = self._final_status(
            report=report,
            structured_facts=structured_facts,
            rejected_rows=rejected_rows,
            unmapped_numeric_evidence=unmapped_numeric_evidence,
            unmapped_table_evidence=unmapped_table_evidence,
            ocr_status=ocr_status,
            extraction_coverage=extraction_coverage,
        )
        statement_family_completeness = self._statement_family_completeness(statement_table_report, structured_facts)
        key_fact_coverage_by_family = self._key_fact_coverage_by_family(statement_table_report, structured_facts)
        engine_fusion_summary = self._engine_fusion_summary(
            statement_table_report=statement_table_report,
            structured_facts=structured_facts,
            rejected_rows=rejected_rows,
            unmapped_numeric_evidence=unmapped_numeric_evidence,
            unmapped_table_evidence=unmapped_table_evidence,
            engine_results=engine_bundle["engine_results"],
        )
        recommended_next_action = self._recommended_next_action(
            final_status=final_status,
            top_blockers=top_blockers,
            structured_facts=structured_facts,
            unmapped_numeric_evidence=unmapped_numeric_evidence,
            unmapped_table_evidence=unmapped_table_evidence,
            engine_results=engine_bundle["engine_results"],
        )
        machine_report = {
            "machine_report_schema_version": SCHEMA_VERSION,
            "pipeline_version": PIPELINE_VERSION,
            "extraction_policy_version": EXTRACTION_POLICY_VERSION,
            "confidence_policy_version": CONFIDENCE_POLICY_VERSION,
            "interpreter_policy_version": INTERPRETER_POLICY_VERSION,
            "evidence_retention_scope": EVIDENCE_RETENTION_SCOPE,
            "document_identity": {
                "company_ticker": report.company_ticker,
                "report_document_id": report.report_document_id,
                "reporting_standard": report.reporting_standard,
                "input_file": report.input_file,
                "stored_document_path": report.stored_document_path,
                "sha256": report.sha256,
                "content_type": report.content_type,
                "source_trust_bucket": report.source_trust_bucket,
                "official_source_verified": report.official_source_verified,
                "source_package_ready_contribution": report.source_package_ready_contribution,
            },
            "document_classification": {
                "ingestion_status": report.ingestion_status,
                "identity_status": report.identity_status,
                "document_classification": report.document_classification,
                "document_validation_status": report.document_validation_status,
                "detected_document_role": report.detected_document_role,
                "ocr_status": ocr_status,
            },
            "period_resolution": {
                "effective_report_period": report.effective_report_period or report.period,
                "comparative_period": report.comparative_period,
                "period_source": report.period_source,
                "period_confidence": report.period_confidence,
                "period_warnings": list(report.period_warnings or []),
                "original_period": report.original_period,
            },
            "processing_status": {
                "final_status": final_status,
                "safe_readable_pdf": bool(report.report_document_id and report.stored_document_path),
                "db_persisted": report.db_persisted,
                "resource_guards": {
                    "document_timeout_seconds": DOCUMENT_TIMEOUT_SECONDS,
                    "page_ocr_timeout_seconds": PAGE_OCR_TIMEOUT_SECONDS,
                    "max_ocr_pages": MAX_OCR_PAGES,
                },
            },
            "runtime_profiles": engine_bundle["runtime_profiles"],
            "engine_cascade_order": engine_bundle["engine_cascade_order"],
            "engines_attempted": engine_bundle["engines_attempted"],
            "engines_succeeded": engine_bundle["engines_succeeded"],
            "engines_failed": engine_bundle["engines_failed"],
            "engine_results": engine_bundle["engine_results"],
            "stage_results": stage_results,
            "source_artifact_provenance": self._source_artifact_provenance(
                report=report,
                statement_table_report=statement_table_report,
                parse_report=parse_report,
                ratios_summary=ratios_summary,
                used_fallback_table_evidence=bool(not parse_report.get("unmapped_table_evidence") and unmapped_table_evidence),
            ),
            "extraction_coverage": extraction_coverage,
            "structured_facts": structured_facts,
            "derived_safe_facts": derived_safe_facts,
            "ratios_summary": ratios_summary,
            "rejected_rows": rejected_rows,
            "unmapped_numeric_evidence": unmapped_numeric_evidence,
            "unmapped_table_evidence": unmapped_table_evidence,
            "analysis_readiness_summary": analysis_readiness,
            "statement_family_completeness": statement_family_completeness,
            "key_fact_coverage_by_family": key_fact_coverage_by_family,
            "engine_fusion_summary": engine_fusion_summary,
            "recovery_actions_attempted": self._recovery_actions_attempted(engine_bundle["engine_results"]),
            "remaining_blockers_after_all_engines": top_blockers,
            "recommended_next_action": recommended_next_action,
            "parser_diagnostics": self._parser_diagnostics(
                report=report,
                validation=validation,
                statement_table_report=statement_table_report,
                parse_report=parse_report,
                ocr_status=ocr_status,
                extraction_coverage=extraction_coverage,
                stage_results=stage_results,
            ),
            "top_blockers": top_blockers,
            "legacy_artifact_links": self._legacy_artifact_links(report, statement_table_report, parse_report, ratios_summary),
        }
        path = machine_report_path(self.root, report.company_ticker, report.effective_report_period or report.period)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(machine_report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return MachineReportBuildResult(payload=machine_report, path=path)

    def _best_available_parse_report(self, report: Any, parse_report: dict[str, Any]) -> dict[str, Any]:
        best = dict(parse_report or {})
        best_score = self._parse_report_score(best)
        for path in self._candidate_parse_report_paths(report, best):
            if not path.exists():
                continue
            try:
                candidate = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            score = self._parse_report_score(candidate)
            if score > best_score:
                best = candidate
                best_score = score
        return best

    def _cached_structural_blockers(self, report: Any, parse_report: dict[str, Any]) -> list[str]:
        if parse_report:
            return []
        return list(getattr(report, "top_structural_blockers", None) or [])

    def _candidate_parse_report_paths(self, report: Any, parse_report: dict[str, Any]) -> list[Path]:
        paths: list[Path] = []
        ticker = str(getattr(report, "company_ticker", "") or "").upper()
        validation_root = self.root / "data" / "validation" / ticker if ticker else None
        period_pairs: list[tuple[str, str]] = []
        period_from = parse_report.get("period_from")
        period_to = parse_report.get("period_to")
        if period_from and period_to:
            period_pairs.append((str(period_from), str(period_to)))
        for period in (
            getattr(report, "effective_report_period", None),
            getattr(report, "period", None),
            getattr(report, "original_period", None),
        ):
            if period:
                period_text = str(period)
                period_pairs.append((period_text, period_text))
        if validation_root:
            for left, right in dict.fromkeys(period_pairs):
                paths.append(validation_root / f"{left}_{right}_dataframe_statement_fact_parse.json")
            if validation_root.exists():
                paths.extend(sorted(validation_root.glob("*_dataframe_statement_fact_parse.json")))
        unique: list[Path] = []
        seen: set[Path] = set()
        for path in paths:
            resolved = path.resolve()
            if resolved not in seen:
                unique.append(path)
                seen.add(resolved)
        return unique

    def _parse_report_score(self, parse_report: dict[str, Any]) -> tuple[int, int, int]:
        structured_count = len(parse_report.get("structured_facts") or [])
        canonical_count = int(parse_report.get("canonical_facts_created") or structured_count or 0)
        evidence_count = (
            len(parse_report.get("rejected_rows") or [])
            + len(parse_report.get("unmapped_numeric_evidence") or [])
            + len(parse_report.get("unmapped_table_evidence") or [])
        )
        return structured_count, canonical_count, evidence_count

    def _load_first_artifact(self, artifacts: list[str]) -> dict[str, Any]:
        for artifact in artifacts:
            path = Path(artifact)
            if not path.is_absolute():
                path = (self.root / artifact).resolve()
            if path.exists():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    return {}
        return {}

    def _ratios_summary(self, report: Any, parse_report: dict[str, Any]) -> dict[str, Any]:
        summary = dict(parse_report.get("ratios_summary") or {})
        report_path = summary.get("report_path")
        if report_path:
            path = Path(report_path)
            if not path.is_absolute():
                path = (self.root / report_path).resolve()
            if path.exists():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    report_summary = payload.get("summary") or {}
                    return {
                        "ratios_report_available": True,
                        "report_path": self._relative(path),
                        "calculated_count": int(report_summary.get("calculated_count") or 0),
                        "missing_count": int(report_summary.get("missing_count") or 0),
                        "unsupported_count": int(report_summary.get("unsupported_count") or 0),
                        "blocked_count": int(report_summary.get("blocked_count") or 0),
                    }
                except Exception:
                    return {
                        "ratios_report_available": False,
                        "report_path": self._relative(path),
                        "ratios_ready": "none",
                    }
        return summary or {"ratios_report_available": False, "report_path": None, "ratios_ready": "none"}

    def _extraction_coverage(self, statement_table_report: dict[str, Any], *, ocr_available: bool) -> dict[str, Any]:
        coverage = dict(statement_table_report.get("extraction_coverage") or {})
        if coverage:
            pages_requiring_ocr = int(coverage.get("pages_requiring_ocr") or 0)
            return {
                "pages_total": int(coverage.get("pages_total") or 0),
                "pages_with_text_layer": int(coverage.get("pages_with_text_layer") or 0),
                "pages_with_table_candidates": int(coverage.get("pages_with_table_candidates") or 0),
                "pages_processed_by_native_extractor": int(coverage.get("pages_processed_by_native_extractor") or 0),
                "pages_requiring_ocr": pages_requiring_ocr,
                "pages_ocr_skipped": pages_requiring_ocr if not ocr_available else int(coverage.get("pages_ocr_skipped") or 0),
                "pages_with_no_usable_extraction": int(coverage.get("pages_with_no_usable_extraction") or 0),
            }
        warnings = list(statement_table_report.get("warnings") or [])
        ocr_pages = sum(
            1
            for warning in warnings
            if str(warning).startswith("primary_statement_page_image_only_or_no_extractable_text")
        )
        tables_found = int(statement_table_report.get("tables_found") or 0)
        return {
            "pages_total": 0,
            "pages_with_text_layer": 0,
            "pages_with_table_candidates": 1 if tables_found else 0,
            "pages_processed_by_native_extractor": 0,
            "pages_requiring_ocr": ocr_pages,
            "pages_ocr_skipped": ocr_pages if not ocr_available else 0,
            "pages_with_no_usable_extraction": 0 if tables_found else (ocr_pages or 0),
        }

    def _ocr_status(
        self,
        statement_table_report: dict[str, Any],
        extraction_coverage: dict[str, Any],
        ocr_available: bool,
    ) -> str:
        _ = statement_table_report
        pages_requiring_ocr = int(extraction_coverage.get("pages_requiring_ocr") or 0)
        if pages_requiring_ocr <= 0:
            return "OCR_NOT_NEEDED"
        if not ocr_available:
            return "OCR_UNAVAILABLE"
        return "OCR_SKIPPED"

    def _ocr_status_from_engine_results(
        self,
        fallback_status: str,
        extraction_coverage: dict[str, Any],
        engine_results: list[dict[str, Any]],
    ) -> str:
        if int(extraction_coverage.get("pages_requiring_ocr") or 0) <= 0:
            return fallback_status
        ocr_engines = {
            "ocrmypdf_engine",
            "page_rasterization_engine",
            "ocr_text_engine",
            "ocr_table_structure_engine",
        }
        relevant = [item for item in engine_results if item.get("engine_name") in ocr_engines]
        if not relevant:
            return fallback_status
        if all(item.get("engine_status") == "UNAVAILABLE" for item in relevant):
            return "OCR_UNAVAILABLE"
        if any(
            item.get("engine_name") in {"ocr_text_engine", "ocr_table_structure_engine"}
            and item.get("engine_status") == "SUCCESS"
            and (int(item.get("table_candidates") or 0) > 0 or int(item.get("text_blocks") or 0) > 0)
            for item in relevant
        ):
            return "OCR_PROCESSED"
        if any(item.get("engine_status") in {"SUCCESS", "PARTIAL", "FAILED"} for item in relevant):
            return "OCR_ATTEMPTED"
        return fallback_status

    def _ocr_blockers(self, ocr_status: str, extraction_coverage: dict[str, Any]) -> list[str]:
        if ocr_status == "OCR_UNAVAILABLE" and int(extraction_coverage.get("pages_requiring_ocr") or 0) > 0:
            return ["ocr_unavailable_for_image_only_or_weak_pages"]
        if ocr_status == "OCR_SKIPPED" and int(extraction_coverage.get("pages_requiring_ocr") or 0) > 0:
            return ["ocr_required_pages_not_processed_in_v1"]
        if ocr_status == "OCR_ATTEMPTED" and int(extraction_coverage.get("pages_requiring_ocr") or 0) > 0:
            return ["ocr_required_pages_attempted_without_confirmed_candidates"]
        return []

    def _merge_warning_based_coverage(
        self,
        coverage: dict[str, Any],
        *,
        table_warnings: list[Any],
        report_warnings: list[Any],
        ocr_available: bool,
    ) -> dict[str, Any]:
        merged = dict(coverage)
        warning_markers = [*table_warnings, *report_warnings]
        pages_requiring_ocr = max(
            int(merged.get("pages_requiring_ocr") or 0),
            sum(
                1
                for warning in warning_markers
                if str(warning).startswith("primary_statement_page_image_only_or_no_extractable_text")
            ),
        )
        merged["pages_requiring_ocr"] = pages_requiring_ocr
        if not ocr_available:
            merged["pages_ocr_skipped"] = max(
                int(merged.get("pages_ocr_skipped") or 0),
                pages_requiring_ocr,
            )
        if pages_requiring_ocr and not merged.get("pages_total"):
            merged["pages_total"] = pages_requiring_ocr
        if pages_requiring_ocr and not merged.get("pages_with_no_usable_extraction"):
            merged["pages_with_no_usable_extraction"] = pages_requiring_ocr
        return merged

    def _parse_stage_blockers(self, parse_report: dict[str, Any]) -> list[str]:
        blockers: list[str] = []
        blockers.extend(list(parse_report.get("period_blockers") or []))
        structural = parse_report.get("structural_blockers") or {}
        for bucket in ("rejected_rows_by_reason", "numeric_evidence_by_reason", "table_evidence_by_reason"):
            for reason, count in (structural.get(bucket) or {}).items():
                if count:
                    blockers.append(str(reason))
        return blockers

    def _stage_results(
        self,
        *,
        report: Any,
        validation: dict[str, Any],
        statement_table_report: dict[str, Any],
        parse_report: dict[str, Any],
        ratios_summary: dict[str, Any],
        ocr_status: str,
        extraction_coverage: dict[str, Any],
        top_blockers: list[str],
    ) -> list[dict[str, Any]]:
        input_file = report.input_file
        stored_path = report.stored_document_path
        validation_stage = {
            "stage_name": "document_validation",
            "status": self._validation_stage_status(report.document_validation_status),
            "action_taken": "validated cached/manual document",
            "input_artifacts": [input_file] if input_file else [],
            "output_artifacts": [stored_path] if stored_path else [],
            "warnings": list(validation.get("warnings") or []),
            "blockers": [] if report.document_validation_status == "pass" else ["document_validation_failed"],
            "exception_summary": None,
        }
        extraction_artifact = statement_table_report.get("artifact_path")
        extraction_stage = {
            "stage_name": "statement_table_extraction",
            "status": self._table_stage_status(report, statement_table_report),
            "action_taken": "native statement table extraction",
            "input_artifacts": [stored_path] if stored_path else [],
            "output_artifacts": [self._relative(Path(extraction_artifact))] if extraction_artifact else [],
            "warnings": list(statement_table_report.get("warnings") or []),
            "blockers": list(report.top_structural_blockers or []),
            "exception_summary": None,
        }
        parse_artifact = self._company_validation_path(
            report.company_ticker,
            f"{parse_report.get('period_from')}_{parse_report.get('period_to')}_dataframe_statement_fact_parse.json",
        ) if parse_report else None
        parse_stage = {
            "stage_name": "fact_extraction",
            "status": self._parse_stage_status(report, parse_report),
            "action_taken": "parsed extracted tables into facts and evidence",
            "input_artifacts": [self._relative(Path(extraction_artifact))] if extraction_artifact else [],
            "output_artifacts": [self._relative(parse_artifact)] if parse_artifact and parse_artifact.exists() else [],
            "warnings": list(parse_report.get("warnings") or []),
            "blockers": self._parse_stage_blockers(parse_report),
            "exception_summary": None,
        }
        ratios_stage = {
            "stage_name": "ratios_import",
            "status": "SUCCESS" if ratios_summary.get("ratios_report_available") else "SKIPPED",
            "action_taken": "imported existing financial ratios report"
            if ratios_summary.get("ratios_report_available")
            else "no ratios report available to import",
            "input_artifacts": [self._relative(parse_artifact)] if parse_artifact and parse_artifact.exists() else [],
            "output_artifacts": [ratios_summary.get("report_path")] if ratios_summary.get("report_path") else [],
            "warnings": [],
            "blockers": [] if ratios_summary.get("ratios_report_available") else ["ratios_report_not_available"],
            "exception_summary": None,
        }
        fallback_stage = {
            "stage_name": "fallback_evidence_collection",
            "status": (
                "SUCCESS"
                if (
                    parse_report.get("unmapped_numeric_evidence")
                    or parse_report.get("unmapped_table_evidence")
                    or statement_table_report
                )
                else "SKIPPED"
            ),
            "action_taken": "retained unresolved evidence from parser/extractor outputs",
            "input_artifacts": [
                item
                for item in [
                    self._relative(Path(extraction_artifact)) if extraction_artifact else None,
                    self._relative(parse_artifact) if parse_artifact and parse_artifact.exists() else None,
                ]
                if item
            ],
            "output_artifacts": [],
            "warnings": [],
            "blockers": [],
            "exception_summary": None,
        }
        ocr_stage = {
            "stage_name": "ocr_fallback",
            "status": "SKIPPED"
            if ocr_status == "OCR_NOT_NEEDED"
            else "FAILED_NON_FATAL"
            if ocr_status == "OCR_UNAVAILABLE"
            else "PARTIAL",
            "action_taken": (
                "ocr not required"
                if ocr_status == "OCR_NOT_NEEDED"
                else "ocr unavailable; continued with native extraction"
                if ocr_status == "OCR_UNAVAILABLE"
                else "ocr-capable pages detected; native-only V1 path retained"
            ),
            "input_artifacts": [stored_path] if stored_path else [],
            "output_artifacts": [],
            "warnings": [],
            "blockers": self._ocr_blockers(ocr_status, extraction_coverage),
            "exception_summary": None,
        }
        return [
            {
                "stage_name": "intake_identity_resolution",
                "status": "SUCCESS" if report.report_document_id else "BLOCKED_FATAL",
                "action_taken": "resolved company identity and accepted manual upload",
                "input_artifacts": [input_file] if input_file else [],
                "output_artifacts": [stored_path] if stored_path else [],
                "warnings": list(report.warnings or []),
                "blockers": list(report.blockers or []),
                "exception_summary": None,
            },
            {
                "stage_name": "file_safety_readability_gate",
                "status": "SUCCESS" if report.report_document_id and report.stored_document_path else "BLOCKED_FATAL",
                "action_taken": "validated file safety and readability prerequisites",
                "input_artifacts": [input_file] if input_file else [],
                "output_artifacts": [stored_path] if stored_path else [],
                "warnings": [],
                "blockers": [] if report.report_document_id and report.stored_document_path else ["invalid_or_unreadable_upload"],
                "exception_summary": None,
            },
            validation_stage,
            {
                "stage_name": "period_resolution",
                "status": "SUCCESS" if report.period_source and report.period_source != "upload_default" else "PARTIAL",
                "action_taken": f"resolved effective period from {report.period_source or 'upload_default'}",
                "input_artifacts": [stored_path] if stored_path else [],
                "output_artifacts": [],
                "warnings": list(report.period_warnings or []),
                "blockers": list(parse_report.get("period_blockers") or []),
                "exception_summary": None,
            },
            extraction_stage,
            ocr_stage,
            parse_stage,
            ratios_stage,
            fallback_stage,
            {
                "stage_name": "final_status_synthesis",
                "status": "SUCCESS",
                "action_taken": "assembled canonical machine report",
                "input_artifacts": [],
                "output_artifacts": [
                    self._relative(
                        machine_report_path(
                            self.root,
                            report.company_ticker,
                            report.effective_report_period or report.period,
                        )
                    )
                ],
                "warnings": [],
                "blockers": top_blockers,
                "exception_summary": None,
            },
        ]

    def _analysis_readiness_summary(
        self,
        *,
        report: Any,
        parse_report: dict[str, Any],
        structured_facts: list[dict[str, Any]],
        derived_safe_facts: list[dict[str, Any]],
        rejected_rows: list[dict[str, Any]],
        unmapped_numeric_evidence: list[dict[str, Any]],
        unmapped_table_evidence: list[dict[str, Any]],
        top_blockers: list[str],
        ocr_status: str,
        extraction_coverage: dict[str, Any],
    ) -> dict[str, Any]:
        summary = dict(parse_report.get("analysis_readiness_summary") or {})
        summary.setdefault("facts_ready", bool(structured_facts))
        summary.setdefault("ratios_ready", "none")
        summary.setdefault(
            "llm_analysis_ready",
            (
                True
                if structured_facts
                else "limited_evidence_only"
                if unmapped_numeric_evidence or unmapped_table_evidence
                else False
            ),
        )
        summary["structured_facts_count"] = len(structured_facts)
        summary["derived_safe_facts_count"] = len(derived_safe_facts)
        summary["rejected_rows_count"] = len(rejected_rows)
        summary["unmapped_numeric_evidence_count"] = len(unmapped_numeric_evidence)
        summary["unmapped_table_evidence_count"] = len(unmapped_table_evidence)
        summary["blocked_areas"] = list(dict.fromkeys([*(summary.get("blocked_areas") or []), *top_blockers]))
        summary["ocr_status"] = ocr_status
        summary["pages_requiring_ocr"] = extraction_coverage.get("pages_requiring_ocr", 0)
        return summary

    def _parser_diagnostics(
        self,
        *,
        report: Any,
        validation: dict[str, Any],
        statement_table_report: dict[str, Any],
        parse_report: dict[str, Any],
        ocr_status: str,
        extraction_coverage: dict[str, Any],
        stage_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        diagnostics = {
            "document_timeout_seconds": DOCUMENT_TIMEOUT_SECONDS,
            "page_ocr_timeout_seconds": PAGE_OCR_TIMEOUT_SECONDS,
            "max_ocr_pages": MAX_OCR_PAGES,
            "ocr_status": ocr_status,
            "stage_failures": [
                {
                    "stage_name": stage["stage_name"],
                    "status": stage["status"],
                    "blockers": stage["blockers"],
                    "exception_summary": stage["exception_summary"],
                }
                for stage in stage_results
                if stage["status"] in {"FAILED_NON_FATAL", "BLOCKED_FATAL", "PARTIAL"}
            ],
            "validator_warnings": list(validation.get("warnings") or []),
            "statement_table_warnings": list(statement_table_report.get("warnings") or []),
            "parse_warnings": list(parse_report.get("warnings") or []),
            "structural_blockers": parse_report.get("structural_blockers") or {},
            "row_parsing_diagnostics": parse_report.get("row_parsing_diagnostics") or {},
            "fact_confidence_summary": parse_report.get("fact_confidence_summary") or {},
            "resource_guards": {
                "pages_ocr_skipped": extraction_coverage.get("pages_ocr_skipped", 0),
                "pages_with_no_usable_extraction": extraction_coverage.get("pages_with_no_usable_extraction", 0),
            },
        }
        if ocr_status == "OCR_UNAVAILABLE" and int(extraction_coverage.get("pages_requiring_ocr") or 0) > 0:
            diagnostics["ocr_limitation"] = (
                "Unreadable image-only or weak pages required OCR, "
                "but PaddleOCR/PP-Structure was unavailable in this environment."
            )
        if ocr_status == "OCR_ATTEMPTED" and int(extraction_coverage.get("pages_requiring_ocr") or 0) > 0:
            diagnostics["ocr_limitation"] = (
                "Unreadable image-only or weak pages required OCR. OCR/table engines were attempted, "
                "but no confirmed statement candidates were produced."
            )
        return diagnostics

    def _statement_family_completeness(
        self,
        statement_table_report: dict[str, Any],
        structured_facts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        coverage = dict(statement_table_report.get("statement_coverage") or {})
        facts_by_statement: dict[str, int] = {}
        for fact in structured_facts:
            statement_type = str(fact.get("statement_type") or "unknown")
            facts_by_statement[statement_type] = facts_by_statement.get(statement_type, 0) + 1
        payload: dict[str, Any] = {}
        for family in ("balance_sheet", "income_statement", "cash_flow"):
            family_coverage = coverage.get(family) or {}
            payload[family] = {
                "found": bool(family_coverage.get("found")),
                "table_count": int(family_coverage.get("count") or 0),
                "structured_fact_count": facts_by_statement.get(family, 0),
                "status": (
                    "complete"
                    if facts_by_statement.get(family, 0) > 0 and family_coverage.get("found")
                    else "detected_without_facts"
                    if family_coverage.get("found")
                    else "missing"
                ),
            }
        return payload

    def _key_fact_coverage_by_family(
        self,
        statement_table_report: dict[str, Any],
        structured_facts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        _ = statement_table_report
        by_family: dict[str, list[str]] = {
            "balance_sheet": [],
            "income_statement": [],
            "cash_flow": [],
        }
        for fact in structured_facts:
            statement_type = str(fact.get("statement_type") or "")
            metric_code = str(fact.get("metric_code") or "")
            if statement_type in by_family and metric_code:
                by_family[statement_type].append(metric_code)
        return {family: sorted(set(metrics)) for family, metrics in by_family.items()}

    def _recovery_actions_attempted(self, engine_results: list[dict[str, Any]]) -> list[str]:
        actions: list[str] = []
        for item in engine_results:
            engine_name = str(item.get("engine_name") or "")
            if engine_name == "native_pdf_table_engine":
                actions.append("native_table_extraction")
            elif engine_name == "native_pdf_text_engine":
                actions.append("native_text_extraction")
            elif engine_name == "word_layout_recovery_engine":
                actions.append("word_layout_structured_recovery")
            elif engine_name == "secondary_table_engine":
                actions.append("secondary_table_engine_probe")
            elif engine_name == "page_rasterization_engine":
                actions.append("page_rasterization_probe")
            elif engine_name == "ocr_text_engine":
                actions.append("ocr_text_probe")
            elif engine_name == "ocr_table_structure_engine":
                actions.append("ocr_table_structure_probe")
        return actions

    def _recommended_next_action(
        self,
        *,
        final_status: str,
        top_blockers: list[str],
        structured_facts: list[dict[str, Any]],
        unmapped_numeric_evidence: list[dict[str, Any]],
        unmapped_table_evidence: list[dict[str, Any]],
        engine_results: list[dict[str, Any]],
    ) -> str:
        if any(item.get("engine_status") == "UNAVAILABLE" for item in engine_results):
            ocr_or_table_unavailable = any(
                str(item.get("engine_name") or "") in {
                    "secondary_table_engine",
                    "page_rasterization_engine",
                    "ocr_text_engine",
                    "ocr_table_structure_engine",
                }
                and item.get("engine_status") == "UNAVAILABLE"
                for item in engine_results
            )
            if ocr_or_table_unavailable and (top_blockers or unmapped_numeric_evidence or unmapped_table_evidence):
                return "install_pdf_heavy_runtime"
        if final_status == "FULL_AUTO_PARSED":
            return "none"
        if structured_facts:
            return "manual_review_recommended"
        if unmapped_numeric_evidence or unmapped_table_evidence or top_blockers:
            return "manual_review_recommended"
        return "upload_alternative_machine_readable_source"

    def _engine_fusion_summary(
        self,
        *,
        statement_table_report: dict[str, Any],
        structured_facts: list[dict[str, Any]],
        rejected_rows: list[dict[str, Any]],
        unmapped_numeric_evidence: list[dict[str, Any]],
        unmapped_table_evidence: list[dict[str, Any]],
        engine_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        diagnostics = list(statement_table_report.get("engine_fusion_diagnostics") or [])
        merged_rows = sum(1 for item in diagnostics if item.get("fusion_status") == "merged_engines")
        conflict_rows = sum(1 for item in diagnostics if item.get("fusion_status") == "conflict_retained_as_evidence")
        merged_fact_count = sum(1 for fact in structured_facts if fact.get("fusion_status") == "merged_engines")
        single_engine_facts = sum(1 for fact in structured_facts if fact.get("fusion_status") == "single_engine")
        ocr_only_fact_count = sum(
            1
            for fact in structured_facts
            if is_ocr_only_fact(fact)
        )
        merged_ocr_fact_count = sum(
            1
            for fact in structured_facts
            if fact.get("fusion_status") == "merged_engines"
            and any("ocr" in str(engine) for engine in list(fact.get("source_engines_involved") or []))
        )
        fact_contribution_by_engine = contribution_counts(structured_facts)
        evidence_items = [*rejected_rows, *unmapped_numeric_evidence, *unmapped_table_evidence]
        evidence_contribution_by_engine = contribution_counts(evidence_items)
        engines_with_fact_contribution = sorted(fact_contribution_by_engine)
        engines_with_evidence_only_contribution = sorted(
            engine
            for engine in evidence_contribution_by_engine
            if engine not in fact_contribution_by_engine
        )
        engine_status_map = {
            str(item.get("engine_name") or ""): str(item.get("engine_status") or "")
            for item in engine_results
        }
        return {
            "merged_rows": merged_rows,
            "conflict_rows_retained_as_evidence": conflict_rows,
            "merged_fact_count": merged_fact_count,
            "single_engine_fact_count": single_engine_facts,
            "ocr_only_fact_count": ocr_only_fact_count,
            "merged_ocr_fact_count": merged_ocr_fact_count,
            "fact_contribution_by_engine": fact_contribution_by_engine,
            "evidence_contribution_by_engine": evidence_contribution_by_engine,
            "engines_with_fact_contribution": engines_with_fact_contribution,
            "engines_with_evidence_only_contribution": engines_with_evidence_only_contribution,
            "engine_status_map": engine_status_map,
            "diagnostics": diagnostics,
        }

    def _legacy_artifact_links(
        self,
        report: Any,
        statement_table_report: dict[str, Any],
        parse_report: dict[str, Any],
        ratios_summary: dict[str, Any],
    ) -> dict[str, Any]:
        links: dict[str, Any] = {}
        if report.identity_report_path:
            links["identity_report"] = report.identity_report_path
        if report.dataframe_artifacts:
            links["statement_table_artifacts"] = [self._relative(Path(item)) for item in report.dataframe_artifacts]
        if parse_report:
            period_from = parse_report.get("period_from")
            period_to = parse_report.get("period_to")
            if period_from and period_to:
                path = self._company_validation_path(
                    report.company_ticker, f"{period_from}_{period_to}_dataframe_statement_fact_parse.json"
                )
                if path.exists():
                    links["dataframe_fact_parse_report"] = self._relative(path)
        if ratios_summary.get("report_path"):
            links["financial_ratios_report"] = ratios_summary.get("report_path")
        manual_path = self._company_validation_path(report.company_ticker, f"{report.period}_manual_report_ingestion.json")
        if manual_path.exists():
            links["manual_ingestion_report"] = self._relative(manual_path)
        return links

    def _source_artifact_provenance(
        self,
        *,
        report: Any,
        statement_table_report: dict[str, Any],
        parse_report: dict[str, Any],
        ratios_summary: dict[str, Any],
        used_fallback_table_evidence: bool,
    ) -> dict[str, Any]:
        extraction_artifact = statement_table_report.get("artifact_path")
        parse_artifact = None
        if parse_report.get("period_from") and parse_report.get("period_to"):
            candidate = self._company_validation_path(
                report.company_ticker,
                f"{parse_report.get('period_from')}_{parse_report.get('period_to')}_dataframe_statement_fact_parse.json",
            )
            parse_artifact = self._relative(candidate) if candidate.exists() else None
        return {
            "validator": {
                "module": "app.services.reports.document_validator.DocumentValidator",
                "input_artifacts": [report.stored_document_path] if report.stored_document_path else [],
                "output_artifacts": [],
                "blocks": ["document_classification", "period_resolution"],
                "provenance_kind": "primary",
            },
            "statement_table_extractor": {
                "module": "app.services.parsing.statement_table_extractor.StatementTableExtractor",
                "input_artifacts": [report.stored_document_path] if report.stored_document_path else [],
                "output_artifacts": [self._relative(Path(extraction_artifact))] if extraction_artifact else [],
                "blocks": ["extraction_coverage", "normalized_statement_tables"],
                "provenance_kind": "primary",
            },
            "dataframe_statement_parser": {
                "module": "app.services.parsing.dataframe_statement_parser.DataFrameStatementParser",
                "input_artifacts": [self._relative(Path(extraction_artifact))] if extraction_artifact else [],
                "output_artifacts": [parse_artifact] if parse_artifact else [],
                "blocks": ["structured_facts", "derived_safe_facts", "rejected_rows", "unmapped_numeric_evidence"],
                "provenance_kind": "primary",
            },
            "financial_ratios_report": {
                "module": "app.services.metrics.financial_ratios_calculator.FinancialRatiosCalculator",
                "input_artifacts": [parse_artifact] if parse_artifact else [],
                "output_artifacts": [ratios_summary.get("report_path")] if ratios_summary.get("report_path") else [],
                "blocks": ["ratios_summary"],
                "provenance_kind": "derived",
            },
            "fallback_evidence_collector": {
                "module": "app.services.reports.machine_report.CanonicalMachineReportBuilder",
                "input_artifacts": [self._relative(Path(extraction_artifact))] if extraction_artifact else [],
                "output_artifacts": [],
                "blocks": ["unmapped_table_evidence"] if used_fallback_table_evidence else [],
                "provenance_kind": "fallback_generated" if used_fallback_table_evidence else "pass_through",
            },
        }

    def _fallback_table_evidence(self, statement_table_report: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for table in statement_table_report.get("statement_tables") or []:
            role = table.get("table_role")
            family = table.get("statement_family")
            if role in {"narrative", "review_only_note"} or family in {"unknown", "notes"}:
                results.append(
                    {
                        "statement_type": table.get("statement_type"),
                        "table_title": table.get("table_title"),
                        "page_number": table.get("page_number"),
                        "rows": list(table.get("rows") or []),
                        "columns": list(table.get("columns") or []),
                        "dataframe_json_compact": table.get("dataframe_json") or {},
                        "evidence_category": "review_only_note"
                        if role == "review_only_note"
                        else "narrative_or_non_numeric"
                        if role == "narrative"
                        else "unknown_table",
                        "evidence_type": "review_only_note_table"
                        if role == "review_only_note"
                        else "unmapped_table",
                        "source_document_id": table.get("document_id"),
                        "source_table_index": table.get("table_index"),
                        "extraction_method": table.get("extraction_method"),
                        "source_trust_bucket": "validated_statement_table"
                        if table.get("eligible_for_fact_normalization")
                        else "manual_upload_candidate_based",
                        "not_confirmed_fact": True,
                        "not_for_ratio_calculation": True,
                        "warnings": list(table.get("warnings") or []),
                        "reasons": [f"table_classified_as_{role or 'unknown'}"],
                    }
                )
        return results

    def _final_status(
        self,
        *,
        report: Any,
        structured_facts: list[dict[str, Any]],
        rejected_rows: list[dict[str, Any]],
        unmapped_numeric_evidence: list[dict[str, Any]],
        unmapped_table_evidence: list[dict[str, Any]],
        ocr_status: str,
        extraction_coverage: dict[str, Any],
    ) -> str:
        useful_evidence = bool(
            structured_facts or rejected_rows or unmapped_numeric_evidence or unmapped_table_evidence
        )
        if (
            ocr_status == "OCR_UNAVAILABLE"
            and int(extraction_coverage.get("pages_requiring_ocr") or 0) > 0
            and not useful_evidence
        ):
            return "AUTO_PARSE_BLOCKED"
        if structured_facts and not (unmapped_numeric_evidence or unmapped_table_evidence or rejected_rows):
            return "FULL_AUTO_PARSED"
        if structured_facts:
            return "PARTIAL_AUTO_PARSED"
        if useful_evidence:
            return "EVIDENCE_ONLY_AUTO_PARSED"
        return "AUTO_PARSE_BLOCKED"

    def _validation_stage_status(self, validation_status: str) -> str:
        if validation_status == "pass":
            return "SUCCESS"
        if validation_status == "partial":
            return "PARTIAL"
        if validation_status == "not_run":
            return "SKIPPED"
        return "BLOCKED_FATAL"

    def _table_stage_status(self, report: Any, statement_table_report: dict[str, Any]) -> str:
        extracted = int(statement_table_report.get("tables_extracted") or statement_table_report.get("tables_found") or 0)
        if extracted and statement_table_report.get("required_statement_tables_found"):
            return "SUCCESS"
        if extracted:
            return "PARTIAL"
        if report.document_validation_status == "not_run":
            return "SKIPPED"
        return "FAILED_NON_FATAL"

    def _parse_stage_status(self, report: Any, parse_report: dict[str, Any]) -> str:
        if not parse_report:
            return "SKIPPED"
        if parse_report.get("status") == "SUCCESS" and parse_report.get("structured_facts"):
            return "SUCCESS"
        if (
            parse_report.get("structured_facts")
            or parse_report.get("unmapped_numeric_evidence")
            or parse_report.get("unmapped_table_evidence")
        ):
            return "PARTIAL"
        if report.fact_parse_status == "not_invoked":
            return "SKIPPED"
        return "FAILED_NON_FATAL"

    def _company_validation_path(self, ticker: str, filename: str) -> Path:
        return self.root / "data" / "validation" / ticker.upper() / filename

    def _relative(self, path: Path | None) -> str | None:
        if path is None:
            return None
        resolved = path.resolve()
        try:
            return str(resolved.relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return str(resolved)


def machine_report_path(root: Path, ticker: str, effective_period: str) -> Path:
    return root / "data" / "validation" / ticker.upper() / f"{effective_period}_machine_report.json"


def paddle_ocr_available() -> bool:
    return importlib.util.find_spec("paddleocr") is not None


def contribution_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        engines = list(item.get("source_engines_involved") or [])
        if not engines and item.get("source_engine"):
            engines = [str(item.get("source_engine"))]
        for engine in engines:
            normalized = str(engine or "").strip()
            if not normalized:
                continue
            counts[normalized] = counts.get(normalized, 0) + 1
    return counts


def is_ocr_only_fact(fact: dict[str, Any]) -> bool:
    engines = list(fact.get("source_engines_involved") or [])
    if not engines and fact.get("source_engine"):
        engines = [str(fact.get("source_engine"))]
    return bool(engines) and all("ocr" in str(engine) for engine in engines)
