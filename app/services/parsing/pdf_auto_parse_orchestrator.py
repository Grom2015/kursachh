from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.services.parsing.dataframe_statement_parser import match_metric
from app.services.parsing.engine_fusion import RowCandidate, fuse_row_candidates
from app.services.parsing.normalized_pdf_layout import (
    build_page_layout_from_candidate,
    merge_page_layouts,
)
from app.services.parsing.pdf_engine_adapters import (
    ENGINE_STATUS_SUCCESS,
    EngineExtractionResult,
    EngineRunContext,
    default_engine_adapters,
    preferred_ocr_document_path,
    runtime_profiles,
)
from app.services.parsing.statement_table_extractor import (
    StatementTableExtractor,
    _table_has_strong_primary_rows,
    build_normalized_row_blocks,
    required_statement_tables_missing,
    statement_coverage,
    summarize_table_period_resolution,
    summarize_table_structure_diagnostics,
)
from app.services.parsing.text_normalization import normalize_matching_text


def run_engine_cascade(context: EngineRunContext) -> list[EngineExtractionResult]:
    results: list[EngineExtractionResult] = []
    for adapter in default_engine_adapters():
        adapter_context = replace(context, prior_engine_results=list(results))
        results.append(adapter.run(adapter_context))
    return results


def synthesize_engine_results(
    *,
    root: Path,
    stored_document_path: str | None,
    statement_table_report: dict[str, Any],
    parse_report: dict[str, Any],
    extraction_coverage: dict[str, Any],
) -> dict[str, Any]:
    cached_results = list(statement_table_report.get("engine_results") or [])
    if cached_results:
        cascade_order = [str(item.get("engine_name") or "") for item in cached_results if item.get("engine_name")]
        attempted = [
            str(item.get("engine_name"))
            for item in cached_results
            if item.get("engine_name") and item.get("engine_status") not in {"SKIPPED", "UNAVAILABLE"}
        ]
        succeeded = [
            str(item.get("engine_name"))
            for item in cached_results
            if item.get("engine_name") and item.get("engine_status") == "SUCCESS"
        ]
        failed = [
            str(item.get("engine_name"))
            for item in cached_results
            if item.get("engine_name") and item.get("engine_status") in {"FAILED", "UNAVAILABLE"}
        ]
        return {
            "runtime_profiles": runtime_profiles(),
            "engine_cascade_order": cascade_order,
            "engines_attempted": attempted,
            "engines_succeeded": succeeded,
            "engines_failed": failed,
            "engine_results": cached_results,
        }
    context = EngineRunContext(
        root=root,
        stored_document_path=stored_document_path,
        statement_table_report=statement_table_report,
        parse_report=parse_report,
        extraction_coverage=extraction_coverage,
        pages_total=int(extraction_coverage.get("pages_total") or 0),
    )
    cascade_results = run_engine_cascade(context)
    results = [result.to_dict() for result in cascade_results]
    cascade_order = [item["engine_name"] for item in results]
    attempted = [item["engine_name"] for item in results if item["engine_status"] not in {"SKIPPED", "UNAVAILABLE"}]
    succeeded = [item["engine_name"] for item in results if item["engine_status"] == "SUCCESS"]
    failed = [
        item["engine_name"]
        for item in results
        if item["engine_status"] in {"FAILED", "UNAVAILABLE"}
    ]
    return {
        "runtime_profiles": runtime_profiles(),
        "engine_cascade_order": cascade_order,
        "engines_attempted": attempted,
        "engines_succeeded": succeeded,
        "engines_failed": failed,
        "engine_results": results,
    }


def augment_statement_table_report_with_engine_candidates(
    *,
    root: Path,
    document: Any,
    statement_table_report: dict[str, Any],
) -> dict[str, Any]:
    extraction_coverage = dict(statement_table_report.get("extraction_coverage") or {})
    context = EngineRunContext(
        root=root,
        stored_document_path=getattr(document, "storage_path", None),
        statement_table_report=statement_table_report,
        parse_report={},
        extraction_coverage=extraction_coverage,
        pages_total=int(extraction_coverage.get("pages_total") or 0),
    )
    cascade_results = run_engine_cascade(context)
    existing_tables = list(
        statement_table_report.get("normalized_statement_tables")
        or statement_table_report.get("statement_tables")
        or []
    )
    extractor = StatementTableExtractor(root=root)
    next_index = max((int(table.get("table_index") or 0) for table in existing_tables), default=-1) + 1
    engine_priority = {item.engine_name: item.engine_priority for item in cascade_results}
    engine_candidate_tables = load_engine_candidate_tables(
        root=root,
        document=document,
        extractor=extractor,
        cascade_results=cascade_results,
        next_index=next_index,
    )
    next_index += len(engine_candidate_tables)
    engine_candidate_tables.extend(
        load_ocr_layer_native_tables(
            root=root,
            document=document,
            extractor=extractor,
            cascade_results=cascade_results,
            next_index=next_index,
            existing_tables=existing_tables,
            statement_table_report=statement_table_report,
        )
    )
    if not engine_candidate_tables:
        updated = attach_engine_results_to_statement_table_report(statement_table_report, cascade_results)
        if updated != statement_table_report:
            StatementTableExtractor(root=root).save_artifact(document, updated)
        return updated
    merged_tables, fusion_diagnostics = merge_engine_candidate_tables(
        existing_tables=existing_tables,
        candidate_tables=engine_candidate_tables,
        engine_priority=engine_priority,
    )
    normalized_page_layouts = collect_normalized_page_layouts(merged_tables, engine_candidate_tables)
    if merged_tables == existing_tables and not fusion_diagnostics:
        return statement_table_report
    updated = attach_engine_results_to_statement_table_report(statement_table_report, cascade_results)
    coverage = statement_coverage(merged_tables)
    missing = required_statement_tables_missing(coverage)
    updated["normalized_statement_tables"] = merged_tables
    updated["statement_tables"] = merged_tables
    updated["tables_found"] = len(merged_tables)
    updated["tables_extracted"] = len(merged_tables)
    updated["statement_tables_count"] = sum(1 for table in merged_tables if table.get("statement_type") != "unknown")
    updated["statement_coverage"] = coverage
    updated["period_resolution"] = summarize_table_period_resolution(merged_tables, document.report_period)
    updated["table_structure_diagnostics"] = summarize_table_structure_diagnostics(merged_tables)
    updated["degraded_primary_statement_count"] = sum(
        1
        for table in merged_tables
        if table.get("table_role") in {"primary_statement_degraded_but_usable", "primary_statement_degraded_unusable"}
    )
    updated["required_statement_tables_found"] = not missing
    updated["required_statement_tables_missing"] = missing
    updated["engine_fusion_diagnostics"] = fusion_diagnostics
    updated["normalized_page_layouts"] = normalized_page_layouts
    updated["warnings"] = [
        *list(statement_table_report.get("warnings") or []),
        "non_native_engine_candidates_processed",
    ]
    StatementTableExtractor().save_artifact(document, updated)
    return updated


def attach_engine_results_to_statement_table_report(
    statement_table_report: dict[str, Any],
    cascade_results: list[EngineExtractionResult],
) -> dict[str, Any]:
    results = [result.to_dict() for result in cascade_results]
    updated = dict(statement_table_report)
    updated["engine_results"] = results
    updated["engine_cascade_order"] = [item["engine_name"] for item in results]
    updated["engines_attempted"] = [
        item["engine_name"] for item in results if item["engine_status"] not in {"SKIPPED", "UNAVAILABLE"}
    ]
    updated["engines_succeeded"] = [item["engine_name"] for item in results if item["engine_status"] == "SUCCESS"]
    updated["engines_failed"] = [
        item["engine_name"] for item in results if item["engine_status"] in {"FAILED", "UNAVAILABLE"}
    ]
    return updated


def should_append_engine_table(existing_tables: list[dict[str, Any]], candidate_table: dict[str, Any]) -> bool:
    same_surface = [
        table
        for table in existing_tables
        if table.get("page_number") == candidate_table.get("page_number")
        and table.get("statement_type") == candidate_table.get("statement_type")
    ]
    if not same_surface:
        return True
    return not any(_table_has_strong_primary_rows(table) for table in same_surface)


def _resolve_artifact_path(root: Path, path_value: str) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else (root / path).resolve()


def load_engine_candidate_tables(
    *,
    root: Path,
    document: Any,
    extractor: StatementTableExtractor,
    cascade_results: list[EngineExtractionResult],
    next_index: int,
) -> list[dict[str, Any]]:
    candidate_tables: list[dict[str, Any]] = []
    for result in cascade_results:
        if result.engine_status != ENGINE_STATUS_SUCCESS or not result.artifact_paths.normalized_candidate_path:
            continue
        candidate_path = _resolve_artifact_path(root, result.artifact_paths.normalized_candidate_path)
        if not candidate_path.exists():
            continue
        try:
            payload = json.loads(candidate_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for candidate in list(payload.get("tables") or []):
            table_dict = build_engine_candidate_table(
                document=document,
                extractor=extractor,
                candidate=candidate,
                engine_name=result.engine_name,
                table_index=next_index,
            )
            next_index += 1
            if not table_dict:
                continue
            candidate_tables.append(table_dict)
    return candidate_tables


def load_ocr_layer_native_tables(
    *,
    root: Path,
    document: Any,
    extractor: StatementTableExtractor,
    cascade_results: list[EngineExtractionResult],
    next_index: int,
    existing_tables: list[dict[str, Any]],
    statement_table_report: dict[str, Any],
) -> list[dict[str, Any]]:
    context = EngineRunContext(
        root=root,
        stored_document_path=getattr(document, "storage_path", None),
        statement_table_report=statement_table_report,
        parse_report={},
        extraction_coverage=dict(statement_table_report.get("extraction_coverage") or {}),
        pages_total=int((statement_table_report.get("extraction_coverage") or {}).get("pages_total") or 0),
        prior_engine_results=cascade_results,
    )
    ocr_path = preferred_ocr_document_path(context)
    if ocr_path is None or not ocr_path.exists():
        return []
    if any("ocr_layer_native_reextract_used" in (table.get("warnings") or []) for table in existing_tables):
        return []
    pages_requiring_ocr = int((statement_table_report.get("extraction_coverage") or {}).get("pages_requiring_ocr") or 0)
    if pages_requiring_ocr <= 0 and not required_statement_tables_missing(statement_coverage(existing_tables)):
        return []
    proxy = extractor._proxy_document(document, str(ocr_path))
    extracted_tables = extractor._extract_pdf(proxy, ocr_path)
    candidate_tables: list[dict[str, Any]] = []
    for offset, table in enumerate(extracted_tables):
        table_dict = table.to_dict()
        source_engine = ocr_layer_source_engine(table_dict)
        source_table_id = str(
            table_dict.get("source_table_id")
            or f"{getattr(document, 'id', 'na')}:{table_dict.get('page_number') or 'na'}:{next_index + offset}"
        )
        table_dict["table_index"] = next_index + offset
        table_dict["source_engine"] = source_engine
        table_dict["source_table_id"] = source_table_id
        table_dict["source_location"] = {
            **dict(table_dict.get("source_location") or {}),
            "source_engine": source_engine,
            "source_table_id": source_table_id,
            "fusion_status": "single_engine",
            "source_engines_involved": [source_engine],
            "ocr_layer_pdf_used": True,
        }
        table_dict["source_traceability"] = {
            **dict(table_dict.get("source_traceability") or {}),
            "source_engine": source_engine,
            "source_page": table_dict.get("page_number"),
            "source_table_id": source_table_id,
            "fusion_status": "single_engine",
            "source_engines_involved": [source_engine],
            "ocr_layer_pdf_used": True,
        }
        table_dict["warnings"] = sorted({*list(table_dict.get("warnings") or []), "ocr_layer_native_reextract_used"})
        row_blocks = []
        for block in list(table_dict.get("row_blocks") or []):
            row_blocks.append(
                {
                    **dict(block),
                    "source_engine": source_engine,
                    "source_page": table_dict.get("page_number"),
                    "source_table_id": source_table_id,
                    "fusion_status": "single_engine",
                    "source_engines_involved": [source_engine],
                    "source_traceability": {
                        **dict(block.get("source_traceability") or {}),
                        "source_engine": source_engine,
                        "source_page": table_dict.get("page_number"),
                        "source_table_id": source_table_id,
                        "fusion_status": "single_engine",
                        "source_engines_involved": [source_engine],
                        "ocr_layer_pdf_used": True,
                    },
                }
            )
        table_dict["row_blocks"] = row_blocks
        candidate_tables.append(table_dict)
    return candidate_tables


def ocr_layer_source_engine(table: dict[str, Any]) -> str:
    extraction_method = str(table.get("extraction_method") or "")
    source_engine = str(table.get("source_engine") or "")
    if source_engine:
        return f"{source_engine}_ocr_layer"
    if extraction_method == "pdf_table":
        return "native_pdf_table_engine_ocr_layer"
    if extraction_method == "text_table_fallback":
        return "text_table_fallback_ocr_layer"
    return "native_pdf_ocr_layer"


def build_engine_candidate_table(
    *,
    document: Any,
    extractor: StatementTableExtractor,
    candidate: dict[str, Any],
    engine_name: str,
    table_index: int,
) -> dict[str, Any] | None:
    row_blocks = list(candidate.get("row_blocks") or [])
    rows = list(candidate.get("rows") or [])
    page_number = candidate.get("page_number")
    page_number = int(page_number) if page_number is not None else None
    if not row_blocks and rows:
        columns = list(candidate.get("header_columns") or candidate.get("columns") or [])
        records = candidate_records_from_rows(rows, columns)
        if records:
            statement_family = str(candidate.get("statement_family") or candidate.get("statement_type") or "unknown")
            row_blocks, row_diagnostics = build_normalized_row_blocks(
                records=records,
                columns=columns or inferred_columns_from_records(records),
                statement_family=statement_family,
                source_engine=engine_name,
                source_page=page_number,
                source_table_id=str(
                    candidate.get("source_table_id")
                    or f"{getattr(document, 'id', 'na')}:{page_number or 'na'}:{table_index}"
                ),
            )
            candidate = {
                **candidate,
                "row_blocks": row_blocks,
                "header_columns": columns or inferred_columns_from_records(records),
                "diagnostics": {
                    **dict(candidate.get("diagnostics") or {}),
                    **dict(row_diagnostics or {}),
                },
            }
            row_blocks = list(candidate.get("row_blocks") or [])
    if row_blocks:
        statement_family = str(candidate.get("statement_family") or "unknown")
        statement_type = str(candidate.get("statement_type") or statement_family)
        header_columns = list(
            candidate.get("header_columns")
            or candidate.get("columns")
            or inferred_columns_from_row_blocks(row_blocks)
        )
        page_layout = build_page_layout_from_candidate(candidate, source_engine=engine_name)
        row_blocks = expand_row_blocks_with_page_layout(
            row_blocks=row_blocks,
            page_layout=page_layout,
            statement_type=statement_type,
            effective_period=str(candidate.get("effective_period") or getattr(document, "report_period", None) or ""),
            comparative_period=str(candidate.get("comparative_period") or ""),
        )
        normalized_rows = materialize_rows_from_row_blocks(row_blocks)
        source_table_id = str(
            candidate.get("source_table_id")
            or f"{getattr(document, 'id', 'na')}:{page_number or 'na'}:{table_index}"
        )
        table_role = str(candidate.get("table_role") or "primary_statement_degraded_but_usable")
        table = {
            "document_id": getattr(document, "id", None),
            "company_ticker": getattr(getattr(document, "company", None), "ticker", ""),
            "period": candidate.get("effective_period") or getattr(document, "report_period", None),
            "effective_period": candidate.get("effective_period") or getattr(document, "report_period", None),
            "comparative_period": candidate.get("comparative_period"),
            "period_source": candidate.get("period_source"),
            "period_confidence": candidate.get("period_confidence"),
            "reporting_standard": getattr(document, "reporting_standard", None),
            "statement_type": statement_type,
            "period_type": candidate.get("period_type") or "annual",
            "table_index": table_index,
            "page_number": page_number,
            "table_title": candidate.get("table_title"),
            "unit": candidate.get("unit"),
            "currency": candidate.get("currency"),
            "unit_multiplier": candidate.get("unit_multiplier"),
            "columns": header_columns,
            "header_columns": header_columns,
            "column_boundaries": list(candidate.get("column_boundaries") or []),
            "rows": normalized_rows,
            "row_blocks": [
                apply_row_block_provenance(block, candidate, engine_name, source_table_id, page_number)
                for block in row_blocks
            ],
            "dataframe_json": {"orientation": "records", "data": normalized_rows},
            "source_location": {
                "page": page_number,
                "table_index": table_index,
                "source_engine": engine_name,
                "source_table_id": source_table_id,
                "source_bbox": candidate.get("source_bbox"),
                "table_bbox": candidate.get("table_bbox"),
                "page_bbox": candidate.get("page_bbox"),
                "fusion_status": "single_engine",
                "source_engines_involved": [engine_name],
            },
            "source_engine": engine_name,
            "source_table_id": source_table_id,
            "source_traceability": {
                "source_engine": engine_name,
                "source_page": page_number,
                "source_table_id": source_table_id,
                "source_bbox": candidate.get("source_bbox"),
                "table_bbox": candidate.get("table_bbox"),
                "page_bbox": candidate.get("page_bbox"),
                "fusion_status": "single_engine",
                "source_engines_involved": [engine_name],
            },
            "confidence_score": float(candidate.get("confidence_score") or 0.75),
            "extraction_method": engine_name,
            "quality_flag": candidate.get("quality_flag") or "engine_candidate_table",
            "warnings": list(candidate.get("warnings") or (["ocr_only_lower_trust"] if "ocr" in engine_name else [])),
            "header_row_count": int(candidate.get("header_row_count") or 1),
            "normalized_columns": header_columns,
            "repeated_column_names_detected": False,
            "label_column_present": True,
            "label_column_reconstructed": bool(candidate.get("label_column_reconstructed")),
            "table_structure_quality": candidate.get("table_structure_quality") or "degraded",
            "period_warnings": list(candidate.get("period_warnings") or []),
            "statement_family": statement_family,
            "table_role": table_role,
            "page_layout": page_layout,
            "statement_title_candidates": list(page_layout.get("statement_title_candidates") or []),
            "layout_diagnostics": dict(page_layout.get("layout_diagnostics") or {}),
            "diagnostics": {
                **dict(candidate.get("diagnostics") or {}),
                "layout_diagnostics": dict(page_layout.get("layout_diagnostics") or {}),
            },
        }
        return table
    if len(rows) < 2:
        return None
    nearby_text = str(candidate.get("nearby_text") or candidate.get("table_title") or "")
    table = extractor._table_from_rows(
        document,
        rows,
        table_index,
        page_number,
        nearby_text,
        table_title=str(candidate.get("table_title") or "") or None,
        extraction_method=engine_name,
    )
    table_dict = table.to_dict()
    for field in (
        "unit",
        "currency",
        "unit_multiplier",
        "effective_period",
        "comparative_period",
        "period_source",
        "period_confidence",
        "period_warnings",
        "statement_family",
        "table_role",
    ):
        if candidate.get(field) is not None:
            table_dict[field] = candidate[field]
    source_table_id = str(
        candidate.get("source_table_id")
        or table_dict.get("source_table_id")
        or f"{getattr(document, 'id', 'na')}:{page_number or 'na'}:{table_index}"
    )
    table_dict["source_engine"] = engine_name
    table_dict["source_table_id"] = source_table_id
    table_dict["source_traceability"] = {
        "source_engine": engine_name,
        "source_page": page_number,
        "source_table_id": source_table_id,
        "source_bbox": candidate.get("source_bbox"),
        "fusion_status": "single_engine",
        "source_engines_involved": [engine_name],
    }
    table_dict["source_location"] = {
        **dict(table_dict.get("source_location") or {}),
        "source_engine": engine_name,
        "source_table_id": source_table_id,
        "source_bbox": candidate.get("source_bbox"),
        "fusion_status": "single_engine",
        "source_engines_involved": [engine_name],
    }
    if "ocr" in engine_name:
        table_dict["warnings"] = sorted({*list(table_dict.get("warnings") or []), "ocr_only_lower_trust"})
    row_blocks = list(table_dict.get("row_blocks") or [])
    table_dict["row_blocks"] = [
        apply_row_block_provenance(block, candidate, engine_name, source_table_id, page_number)
        for block in row_blocks
    ]
    page_layout = build_page_layout_from_candidate(candidate, source_engine=engine_name)
    table_dict["page_layout"] = page_layout
    table_dict["statement_title_candidates"] = list(page_layout.get("statement_title_candidates") or [])
    table_dict["layout_diagnostics"] = dict(page_layout.get("layout_diagnostics") or {})
    return table_dict


def collect_normalized_page_layouts(
    merged_tables: list[dict[str, Any]],
    candidate_tables: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    page_layouts = [
        dict(table.get("page_layout") or {})
        for table in merged_tables
        if isinstance(table.get("page_layout"), dict) and table.get("page_layout")
    ]
    page_layouts.extend(
        build_page_layout_from_candidate(candidate, source_engine=str(candidate.get("source_engine") or "candidate"))
        for candidate in candidate_tables
    )
    return merge_page_layouts([layout for layout in page_layouts if layout])


def candidate_records_from_rows(rows: list[Any], columns: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    normalized_columns = [str(column) for column in columns]
    for row in rows:
        if isinstance(row, dict):
            records.append({str(key): value for key, value in row.items()})
            continue
        if isinstance(row, (list, tuple)):
            if not normalized_columns:
                continue
            record = {
                normalized_columns[index]: row[index]
                for index in range(min(len(normalized_columns), len(row)))
            }
            if record:
                records.append(record)
    return records


def inferred_columns_from_records(records: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    seen: set[str] = set()
    for record in records:
        for key in record.keys():
            key_str = str(key)
            if key_str not in seen:
                seen.add(key_str)
                columns.append(key_str)
    return columns


def apply_row_block_provenance(
    row_block: dict[str, Any],
    candidate: dict[str, Any],
    engine_name: str,
    source_table_id: str,
    page_number: int | None,
) -> dict[str, Any]:
    block = dict(row_block)
    warnings = list((block.get("diagnostics") or {}).get("warnings") or [])
    if "ocr" in engine_name and "ocr_only_lower_trust" not in warnings:
        warnings.append("ocr_only_lower_trust")
    block["source_engine"] = str(block.get("source_engine") or candidate.get("source_engine") or engine_name)
    block["source_page"] = block.get("source_page") if block.get("source_page") is not None else page_number
    block["source_table_id"] = str(block.get("source_table_id") or candidate.get("source_table_id") or source_table_id)
    block["source_bbox"] = block.get("source_bbox") if block.get("source_bbox") is not None else candidate.get("source_bbox")
    if block.get("label_bbox") is None and candidate.get("label_bbox") is not None:
        block["label_bbox"] = candidate.get("label_bbox")
    if not block.get("value_bboxes") and candidate.get("value_bboxes") is not None:
        block["value_bboxes"] = candidate.get("value_bboxes")
    block["fusion_status"] = str(block.get("fusion_status") or "single_engine")
    block["source_engines_involved"] = list(block.get("source_engines_involved") or [block["source_engine"]])
    diagnostics = dict(block.get("diagnostics") or {})
    diagnostics["warnings"] = warnings
    diagnostics.setdefault("fragment_role", partial_fragment_role(block))
    diagnostics.setdefault("anchor_hints", partial_fragment_anchor_hints(block))
    block["diagnostics"] = diagnostics
    block["source_traceability"] = {
        "source_engine": block["source_engine"],
        "source_page": block["source_page"],
        "source_table_id": block["source_table_id"],
        "source_bbox": block["source_bbox"],
        "fusion_status": block["fusion_status"],
        "source_engines_involved": block["source_engines_involved"],
    }
    return block


def partial_fragment_role(row_block: dict[str, Any]) -> str:
    label_text = str(row_block.get("label_text") or "").strip()
    has_values = row_block_has_values(row_block)
    if label_text and has_values:
        return "mixed_fragment"
    if label_text:
        return "label_only"
    if has_values:
        return "value_only"
    return "empty_fragment"


def partial_fragment_anchor_hints(row_block: dict[str, Any]) -> list[str]:
    label_text = str(row_block.get("label_text") or "").strip()
    row_kind = str(row_block.get("row_kind") or "")
    hints: list[str] = []
    role = partial_fragment_role(row_block)
    if role == "value_only":
        hints.append("attach_to_previous_label_only_row")
    if role == "mixed_fragment" and is_note_reference_like_label(label_text):
        hints.append("treat_label_as_note_reference")
        hints.append("attach_to_previous_label_only_row")
    if role == "mixed_fragment" and row_kind == "numeric_fragment":
        hints.append("prefer_adjacent_numeric_tail_merge")
    if role == "mixed_fragment" and candidate_is_continuation_fragment(normalize_merge_label(label_text)):
        hints.append("prefer_label_continuation_merge")
    return hints


def merge_engine_candidate_tables(
    *,
    existing_tables: list[dict[str, Any]],
    candidate_tables: list[dict[str, Any]],
    engine_priority: dict[str, int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    merged_tables = [dict(table) for table in existing_tables]
    fusion_diagnostics: list[dict[str, Any]] = []
    for candidate_table in candidate_tables:
        same_surface_indexes = matching_surface_indexes(merged_tables, candidate_table)
        if not same_surface_indexes:
            if str(candidate_table.get("statement_type") or "unknown") == "unknown":
                continue
            if should_append_engine_table(merged_tables, candidate_table):
                merged_tables.append(materialize_table_from_row_blocks(candidate_table))
            continue
        target_index = same_surface_indexes[0]
        target_table = merged_tables[target_index]
        merged_table, diagnostics = fuse_table_row_blocks(
            base_table=target_table,
            candidate_table=candidate_table,
            engine_priority=engine_priority,
        )
        merged_tables[target_index] = merged_table
        fusion_diagnostics.extend(diagnostics)
    return merged_tables, fusion_diagnostics


def fuse_table_row_blocks(
    *,
    base_table: dict[str, Any],
    candidate_table: dict[str, Any],
    engine_priority: dict[str, int],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    merged_table = materialize_table_from_row_blocks(base_table)
    base_blocks = [dict(block) for block in list(merged_table.get("row_blocks") or [])]
    candidate_blocks = [dict(block) for block in list(candidate_table.get("row_blocks") or [])]
    diagnostics: list[dict[str, Any]] = []
    table_conflict_reason = table_context_conflict_reason(base_table, candidate_table)
    if table_conflict_reason:
        for candidate_block in candidate_blocks:
            conflict_block = mark_conflict_row_block(
                candidate_block,
                conflict_reason=table_conflict_reason,
                source_engines_involved=collect_table_engines(base_table, candidate_table),
            )
            base_blocks.append(conflict_block)
            diagnostics.append(
                {
                    "page_number": merged_table.get("page_number"),
                    "statement_type": merged_table.get("statement_type"),
                    "row_label": conflict_block.get("label_text"),
                    "fusion_status": "conflict_retained_as_evidence",
                    "conflict_reason": table_conflict_reason,
                    "source_engines_involved": conflict_block.get("source_engines_involved") or [],
                    "conflict_scope": "table_context",
                }
            )
        merged_table["row_blocks"] = base_blocks
        merged_table.setdefault("diagnostics", {})
        merged_table["diagnostics"]["engine_conflicts_detected"] = True
        merged_table["diagnostics"]["engine_fusion_events"] = diagnostics
        merged_table["warnings"] = sorted(
            {
                *list(merged_table.get("warnings") or []),
                "table_context_conflict_retained_as_evidence",
                *[
                    warning
                    for block in base_blocks
                    for warning in list((block.get("diagnostics") or {}).get("warnings") or [])
                ],
            }
        )
        return materialize_table_from_row_blocks(merged_table), diagnostics
    for candidate_block in candidate_blocks:
        match_index = best_row_block_match_index(base_blocks, candidate_block)
        if match_index is None:
            anchor_index = adjacent_merge_anchor_index(base_blocks, candidate_block)
            if anchor_index is None:
                base_blocks.append(candidate_block)
            else:
                base_blocks.insert(anchor_index + 1, candidate_block)
            continue
        base_block = base_blocks[match_index]
        if should_merge_continuation_row_blocks(base_block, candidate_block):
            base_blocks[match_index] = merge_continuation_row_blocks(base_block, candidate_block)
            diagnostics.append(
                {
                    "page_number": merged_table.get("page_number"),
                    "statement_type": merged_table.get("statement_type"),
                    "row_label": base_blocks[match_index].get("label_text"),
                    "fusion_status": "merged_engines",
                    "source_engines_involved": list(base_blocks[match_index].get("source_engines_involved") or []),
                    "fusion_mode": "continuation_row_merge",
                }
            )
            continue
        if should_merge_semantic_identity_row_blocks(base_block, candidate_block):
            base_blocks[match_index] = merge_semantic_identity_row_blocks(base_block, candidate_block)
            diagnostics.append(
                {
                    "page_number": merged_table.get("page_number"),
                    "statement_type": merged_table.get("statement_type"),
                    "row_label": base_blocks[match_index].get("label_text"),
                    "fusion_status": "merged_engines",
                    "source_engines_involved": list(base_blocks[match_index].get("source_engines_involved") or []),
                    "fusion_mode": "semantic_row_identity_merge",
                }
            )
            continue
        decision = fuse_row_candidates(
            [
                row_block_to_candidate(base_table, base_block),
                row_block_to_candidate(candidate_table, candidate_block),
            ],
            engine_priority=engine_priority,
        )
        if decision.allowed and decision.merged_candidate is not None:
            base_blocks[match_index] = apply_fusion_decision_to_row_block(base_block, candidate_block, decision)
            diagnostics.append(
                {
                    "page_number": merged_table.get("page_number"),
                    "statement_type": merged_table.get("statement_type"),
                    "row_label": base_blocks[match_index].get("label_text"),
                    "fusion_status": decision.fusion_status,
                    "source_engines_involved": decision.source_engines_involved,
                }
            )
            continue
        conflict_block = mark_conflict_row_block(candidate_block, decision)
        base_blocks.append(conflict_block)
        diagnostics.append(
            {
                "page_number": merged_table.get("page_number"),
                "statement_type": merged_table.get("statement_type"),
                "row_label": conflict_block.get("label_text"),
                "fusion_status": decision.fusion_status,
                "conflict_reason": decision.reason,
                "source_engines_involved": decision.source_engines_involved,
            }
        )
    merged_table["row_blocks"] = base_blocks
    merged_table = merge_table_context(merged_table, candidate_table)
    union_engines = sorted(
        {
            engine
            for block in base_blocks
            for engine in list(block.get("source_engines_involved") or [])
            if engine
        }
    )
    traceability = dict(merged_table.get("source_traceability") or {})
    traceability["fusion_status"] = (
        "merged_engines" if len(union_engines) > 1 else traceability.get("fusion_status", "single_engine")
    )
    traceability["source_engines_involved"] = union_engines or list(traceability.get("source_engines_involved") or [])
    merged_table["source_traceability"] = traceability
    merged_table.setdefault("diagnostics", {})
    merged_table["diagnostics"]["engine_conflicts_detected"] = any(
        item.get("fusion_status") == "conflict_retained_as_evidence" for item in diagnostics
    )
    merged_table["diagnostics"]["engine_fusion_events"] = diagnostics
    merged_table["warnings"] = sorted(
        {
            *list(merged_table.get("warnings") or []),
            *[
                warning
                for block in base_blocks
                for warning in list((block.get("diagnostics") or {}).get("warnings") or [])
            ],
        }
    )
    return materialize_table_from_row_blocks(merged_table), diagnostics


def best_row_block_match_index(existing_blocks: list[dict[str, Any]], candidate_block: dict[str, Any]) -> int | None:
    candidate_label = str(candidate_block.get("label_text") or "").strip().casefold()
    candidate_kind = str(candidate_block.get("row_kind") or "")
    candidate_identity = semantic_row_identity(candidate_block)
    for index, block in enumerate(existing_blocks):
        existing_label = str(block.get("label_text") or "").strip().casefold()
        existing_kind = str(block.get("row_kind") or "")
        if candidate_kind and existing_kind and candidate_kind != existing_kind:
            continue
        if candidate_label and existing_label and candidate_label == existing_label:
            return index
        if semantic_row_identities_match(semantic_row_identity(block), candidate_identity):
            return index
        if should_merge_continuation_row_blocks(block, candidate_block):
            return index
        if not existing_label and candidate_label:
            return index
        if existing_label and not candidate_label:
            return index
    return None


def matching_surface_indexes(existing_tables: list[dict[str, Any]], candidate_table: dict[str, Any]) -> list[int]:
    candidate_page = candidate_table.get("page_number")
    candidate_type = str(candidate_table.get("statement_type") or "")
    exact = [
        index
        for index, table in enumerate(existing_tables)
        if table.get("page_number") == candidate_page
        and table.get("statement_type") == candidate_type
    ]
    if exact:
        return exact
    if candidate_type and candidate_type != "unknown":
        return []
    same_page_primary = [
        index
        for index, table in enumerate(existing_tables)
        if table.get("page_number") == candidate_page
        and str(table.get("statement_type") or "unknown") != "unknown"
        and str(table.get("table_role") or "").startswith("primary_statement")
    ]
    if len(same_page_primary) == 1:
        return same_page_primary
    return []


def adjacent_merge_anchor_index(existing_blocks: list[dict[str, Any]], candidate_block: dict[str, Any]) -> int | None:
    candidate_label = str(candidate_block.get("label_text") or "").strip()
    candidate_kind = str(candidate_block.get("row_kind") or "")
    candidate_has_values = row_block_has_values(candidate_block)
    note_like_candidate = is_note_reference_like_label(candidate_label)
    candidate_hints = list((candidate_block.get("diagnostics") or {}).get("anchor_hints") or [])
    if candidate_kind and candidate_kind not in {"statement_line_item", "numeric_fragment", "note_reference_only"}:
        return None
    if not candidate_has_values:
        return None
    for index, block in enumerate(existing_blocks):
        existing_kind = str(block.get("row_kind") or "")
        if existing_kind and existing_kind not in {"statement_line_item", "numeric_fragment"}:
            continue
        if row_block_has_values(block):
            continue
        existing_label = str(block.get("label_text") or "").strip()
        if not existing_label:
            continue
        if not candidate_label or note_like_candidate or "attach_to_previous_label_only_row" in candidate_hints:
            return index
        if is_continuation_merge_candidate(existing_label, candidate_label):
            return index
    return None


def is_note_reference_like_label(label: str | None) -> bool:
    cleaned = str(label or "").strip()
    if not cleaned:
        return False
    compact = re.sub(r"[\s().,]+", "", cleaned.casefold())
    if compact.isdigit() and len(compact) <= 3:
        return True
    normalized = cleaned.casefold()
    note_markers = ("note", "notes", "прим", "примеч", "поясн")
    return any(marker in normalized for marker in note_markers) and any(char.isdigit() for char in normalized)


def row_block_to_candidate(table: dict[str, Any], row_block: dict[str, Any]) -> RowCandidate:
    current_value = first_numeric_value(row_block.get("value_cells") or {})
    comparative_value = second_numeric_value(row_block.get("value_cells") or {})
    table_traceability = dict(table.get("source_traceability") or {})
    statement_family = normalized_statement_family(table) or None
    return RowCandidate(
        source_engine=str(
            row_block.get("source_engine") or table_traceability.get("source_engine") or table.get("extraction_method")
        ),
        source_page=row_block.get("source_page") if row_block.get("source_page") is not None else table.get("page_number"),
        source_table_id=str(
            row_block.get("source_table_id") or table_traceability.get("source_table_id") or table.get("source_table_id") or ""
        ),
        source_bbox=row_block.get("source_bbox"),
        label_text=str(row_block.get("label_text") or "").strip() or None,
        current_value=current_value,
        comparative_value=comparative_value,
        effective_period=table.get("effective_period") or table.get("period"),
        comparative_period=table.get("comparative_period"),
        statement_family=statement_family,
        row_kind=row_block.get("row_kind"),
        sign_hint=sign_hint_for_value(current_value),
        confidence=float(row_block.get("row_confidence") or 0.0),
        lower_trust_warning=first_warning(row_block),
    )


def apply_fusion_decision_to_row_block(
    base_block: dict[str, Any],
    candidate_block: dict[str, Any],
    decision: Any,
) -> dict[str, Any]:
    merged = dict(base_block)
    merged_candidate = decision.merged_candidate
    if merged_candidate is None:
        return merged
    merged["label_text"] = merged_candidate.label_text
    merged["label_tokens"] = [token for token in str(merged_candidate.label_text or "").split() if token]
    merged["row_confidence"] = max(
        float(base_block.get("row_confidence") or 0.0),
        float(candidate_block.get("row_confidence") or 0.0),
    )
    merged["fusion_status"] = decision.fusion_status
    merged["source_engines_involved"] = decision.source_engines_involved
    merged["source_engine"] = merged_candidate.source_engine
    merged["source_page"] = merged_candidate.source_page
    merged["source_table_id"] = merged_candidate.source_table_id
    merged["source_bbox"] = merged_candidate.source_bbox
    merged["value_cells"] = merge_value_cells(base_block.get("value_cells") or {}, candidate_block.get("value_cells") or {})
    diagnostics = dict(base_block.get("diagnostics") or {})
    candidate_diagnostics = dict(candidate_block.get("diagnostics") or {})
    warnings = sorted(
        {
            *list(diagnostics.get("warnings") or []),
            *list(candidate_diagnostics.get("warnings") or []),
            *(["ocr_only_lower_trust"] if decision.warning else []),
        }
    )
    diagnostics["warnings"] = warnings
    diagnostics["fusion_warning"] = decision.warning
    diagnostics["fusion_status"] = decision.fusion_status
    merged["diagnostics"] = diagnostics
    merged["source_traceability"] = {
        "source_engine": merged["source_engine"],
        "source_page": merged["source_page"],
        "source_table_id": merged["source_table_id"],
        "source_bbox": merged["source_bbox"],
        "fusion_status": merged["fusion_status"],
        "source_engines_involved": merged["source_engines_involved"],
    }
    return merged


def should_merge_continuation_row_blocks(base_block: dict[str, Any], candidate_block: dict[str, Any]) -> bool:
    if row_block_has_values(base_block):
        return False
    if not row_block_has_values(candidate_block):
        return False
    base_kind = str(base_block.get("row_kind") or "")
    candidate_kind = str(candidate_block.get("row_kind") or "")
    if base_kind and candidate_kind and base_kind != candidate_kind:
        return False
    base_label = str(base_block.get("label_text") or "").strip()
    candidate_label = str(candidate_block.get("label_text") or "").strip()
    if not base_label or not candidate_label:
        return False
    return is_continuation_merge_candidate(base_label, candidate_label)


def merge_continuation_row_blocks(base_block: dict[str, Any], candidate_block: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base_block)
    base_label = str(base_block.get("label_text") or "").strip()
    candidate_label = str(candidate_block.get("label_text") or "").strip()
    merged_label = " ".join(part for part in [base_label, candidate_label] if part).strip()
    merged["label_text"] = merged_label
    merged["label_tokens"] = [token for token in merged_label.split() if token]
    merged["value_cells"] = merge_value_cells(base_block.get("value_cells") or {}, candidate_block.get("value_cells") or {})
    merged["row_confidence"] = max(
        float(base_block.get("row_confidence") or 0.0),
        float(candidate_block.get("row_confidence") or 0.0),
    )
    source_engines = sorted(
        {
            *list(base_block.get("source_engines_involved") or []),
            *list(candidate_block.get("source_engines_involved") or []),
            str(base_block.get("source_engine") or ""),
            str(candidate_block.get("source_engine") or ""),
        }
        - {""}
    )
    merged["fusion_status"] = "merged_engines"
    merged["source_engines_involved"] = source_engines
    merged["source_engine"] = str(base_block.get("source_engine") or candidate_block.get("source_engine") or "")
    merged["source_page"] = (
        base_block.get("source_page")
        if base_block.get("source_page") is not None
        else candidate_block.get("source_page")
    )
    merged["source_table_id"] = str(
        base_block.get("source_table_id") or candidate_block.get("source_table_id") or ""
    )
    merged["source_bbox"] = merge_token_bboxes([base_block.get("source_bbox"), candidate_block.get("source_bbox")])
    diagnostics = {
        **dict(base_block.get("diagnostics") or {}),
        **dict(candidate_block.get("diagnostics") or {}),
    }
    diagnostics["warnings"] = sorted(
        {
            *list((base_block.get("diagnostics") or {}).get("warnings") or []),
            *list((candidate_block.get("diagnostics") or {}).get("warnings") or []),
            *(
                ["ocr_only_lower_trust"]
                if any("ocr" in engine for engine in source_engines)
                and not any("native_pdf" in engine for engine in source_engines)
                else []
            ),
        }
    )
    diagnostics["fusion_status"] = "merged_engines"
    diagnostics["fusion_mode"] = "continuation_row_merge"
    merged["diagnostics"] = diagnostics
    merged["source_traceability"] = {
        "source_engine": merged["source_engine"],
        "source_page": merged["source_page"],
        "source_table_id": merged["source_table_id"],
        "source_bbox": merged["source_bbox"],
        "fusion_status": merged["fusion_status"],
        "source_engines_involved": source_engines,
    }
    return merged


def should_merge_semantic_identity_row_blocks(base_block: dict[str, Any], candidate_block: dict[str, Any]) -> bool:
    if not row_block_has_values(base_block) or not row_block_has_values(candidate_block):
        return False
    return semantic_row_identities_match(semantic_row_identity(base_block), semantic_row_identity(candidate_block))


def merge_semantic_identity_row_blocks(base_block: dict[str, Any], candidate_block: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base_block)
    base_label = str(base_block.get("label_text") or "").strip()
    candidate_label = str(candidate_block.get("label_text") or "").strip()
    merged["label_text"] = choose_canonical_merged_label(base_label, candidate_label)
    merged["label_tokens"] = [token for token in str(merged["label_text"] or "").split() if token]
    merged["value_cells"] = merge_value_cells(base_block.get("value_cells") or {}, candidate_block.get("value_cells") or {})
    merged["row_confidence"] = max(
        float(base_block.get("row_confidence") or 0.0),
        float(candidate_block.get("row_confidence") or 0.0),
    )
    source_engines = sorted(
        {
            *list(base_block.get("source_engines_involved") or []),
            *list(candidate_block.get("source_engines_involved") or []),
            str(base_block.get("source_engine") or ""),
            str(candidate_block.get("source_engine") or ""),
        }
        - {""}
    )
    merged["fusion_status"] = "merged_engines"
    merged["source_engines_involved"] = source_engines
    merged["source_engine"] = str(base_block.get("source_engine") or candidate_block.get("source_engine") or "")
    merged["source_page"] = (
        base_block.get("source_page")
        if base_block.get("source_page") is not None
        else candidate_block.get("source_page")
    )
    merged["source_table_id"] = str(base_block.get("source_table_id") or candidate_block.get("source_table_id") or "")
    merged["source_bbox"] = merge_token_bboxes([base_block.get("source_bbox"), candidate_block.get("source_bbox")])
    diagnostics = {
        **dict(base_block.get("diagnostics") or {}),
        **dict(candidate_block.get("diagnostics") or {}),
    }
    diagnostics["warnings"] = sorted(
        {
            *list((base_block.get("diagnostics") or {}).get("warnings") or []),
            *list((candidate_block.get("diagnostics") or {}).get("warnings") or []),
        }
    )
    diagnostics["fusion_status"] = "merged_engines"
    diagnostics["fusion_mode"] = "semantic_row_identity_merge"
    merged["diagnostics"] = diagnostics
    merged["source_traceability"] = {
        "source_engine": merged["source_engine"],
        "source_page": merged["source_page"],
        "source_table_id": merged["source_table_id"],
        "source_bbox": merged["source_bbox"],
        "fusion_status": merged["fusion_status"],
        "source_engines_involved": source_engines,
    }
    return merged


def choose_canonical_merged_label(left: str, right: str) -> str:
    if len(str(right or "").strip()) > len(str(left or "").strip()):
        return right
    return left


def mark_conflict_row_block(
    candidate_block: dict[str, Any],
    decision: Any | None = None,
    *,
    conflict_reason: str | None = None,
    source_engines_involved: list[str] | None = None,
) -> dict[str, Any]:
    conflict = dict(candidate_block)
    reason = conflict_reason or (decision.reason if decision is not None else "engine_conflict_retained_as_evidence")
    engines = source_engines_involved or (
        decision.source_engines_involved if decision is not None else list(conflict.get("source_engines_involved") or [])
    )
    conflict["fusion_status"] = "conflict_retained_as_evidence"
    conflict["source_engines_involved"] = engines
    diagnostics = dict(conflict.get("diagnostics") or {})
    diagnostics["fusion_conflict_reason"] = reason
    diagnostics["warnings"] = sorted(
        {
            *list(diagnostics.get("warnings") or []),
            "engine_conflict_retained_as_evidence",
        }
    )
    conflict["diagnostics"] = diagnostics
    conflict["source_traceability"] = {
        "source_engine": conflict.get("source_engine"),
        "source_page": conflict.get("source_page"),
        "source_table_id": conflict.get("source_table_id"),
        "source_bbox": conflict.get("source_bbox"),
        "fusion_status": conflict["fusion_status"],
        "source_engines_involved": conflict["source_engines_involved"],
    }
    return conflict


def materialize_table_from_row_blocks(table: dict[str, Any]) -> dict[str, Any]:
    materialized = dict(table)
    row_blocks = expand_row_blocks_with_page_layout(
        row_blocks=list(materialized.get("row_blocks") or []),
        page_layout=dict(materialized.get("page_layout") or {}),
        statement_type=str(materialized.get("statement_type") or materialized.get("statement_family") or ""),
        effective_period=str(materialized.get("effective_period") or materialized.get("period") or ""),
        comparative_period=str(materialized.get("comparative_period") or ""),
    )
    materialized["row_blocks"] = row_blocks
    rows = materialize_rows_from_row_blocks(row_blocks)
    materialized["rows"] = rows
    materialized["dataframe_json"] = {"orientation": "records", "data": rows}
    return materialized


def materialize_rows_from_row_blocks(row_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [normalized_row_from_row_block(block) for block in row_blocks]


def expand_row_blocks_with_page_layout(
    *,
    row_blocks: list[dict[str, Any]],
    page_layout: dict[str, Any],
    statement_type: str,
    effective_period: str,
    comparative_period: str,
) -> list[dict[str, Any]]:
    if statement_type not in {"income_statement", "cash_flow"}:
        return row_blocks
    expanded: list[dict[str, Any]] = []
    for row_index, row_block in enumerate(row_blocks):
        row_block = recover_row_block_label_from_layout_tokens(
            row_block=row_block,
            row_index=row_index,
            page_layout=page_layout,
            statement_type=statement_type,
        )
        diagnostics = dict(row_block.get("diagnostics") or {})
        if diagnostics.get("merged_statement_line_split"):
            expanded.append(row_block)
            continue
        split_blocks = split_row_block_from_page_layout(
            row_block=row_block,
            row_index=row_index,
            page_layout=page_layout,
            statement_type=statement_type,
            effective_period=effective_period,
            comparative_period=comparative_period,
        )
        if split_blocks:
            expanded.extend(split_blocks)
            continue
        expanded.append(row_block)
    return expanded


def recover_row_block_label_from_layout_tokens(
    *,
    row_block: dict[str, Any],
    row_index: int,
    page_layout: dict[str, Any],
    statement_type: str,
) -> dict[str, Any]:
    if str(row_block.get("label_text") or "").strip():
        return row_block
    if not row_block_has_values(row_block):
        return row_block
    if not row_block.get("value_bboxes"):
        return row_block
    label_tokens = aligned_label_tokens_for_row_block(row_block, row_index, page_layout)
    if not label_tokens:
        return row_block
    label_text = " ".join(token["text"] for token in label_tokens if str(token.get("text") or "").strip()).strip()
    if not label_text or not is_statement_like_label(label_text, statement_type):
        return row_block
    recovered = dict(row_block)
    recovered["label_text"] = label_text
    recovered["label_tokens"] = label_text.split()
    recovered["label_bbox"] = merge_token_bboxes([token.get("bbox") for token in label_tokens if token.get("bbox") is not None])
    recovered["label_confidence"] = max(float(recovered.get("label_confidence") or 0.0), 0.86)
    recovered["ownership_confidence"] = max(float(recovered.get("ownership_confidence") or 0.0), 0.84)
    recovered["fragment_role"] = "recovered_statement_row"
    diagnostics = dict(recovered.get("diagnostics") or {})
    anchor_hints = list(diagnostics.get("anchor_hints") or [])
    if "layout_token_label_alignment" not in anchor_hints:
        anchor_hints.append("layout_token_label_alignment")
    diagnostics["anchor_hints"] = anchor_hints
    diagnostics["label_recovered_from_layout_tokens"] = True
    diagnostics["recovery_mode"] = "layout_token_label_alignment"
    recovered["diagnostics"] = diagnostics
    return recovered


def split_row_block_from_page_layout(
    *,
    row_block: dict[str, Any],
    row_index: int,
    page_layout: dict[str, Any],
    statement_type: str,
    effective_period: str,
    comparative_period: str,
) -> list[dict[str, Any]] | None:
    if row_block_has_values(row_block):
        return None
    source_line = str((row_block.get("diagnostics") or {}).get("source_line") or row_block.get("label_text") or "").strip()
    if not source_line:
        return None
    segments = split_statement_line_segments(source_line, statement_type)
    if len(segments) < 2:
        segments = split_statement_line_segments_from_layout_tokens(
            page_layout=page_layout,
            row_index=row_index,
            statement_type=statement_type,
        )
    if len(segments) < 2:
        return None
    current_key, comparative_key = inline_period_columns_for_layout(effective_period, comparative_period)
    if not current_key:
        return None
    result: list[dict[str, Any]] = []
    for segment_index, (label, numbers) in enumerate(segments):
        split_block = dict(row_block)
        split_block["label_text"] = label
        split_block["label_tokens"] = label.split()
        value_cells: dict[str, Any] = {current_key: numbers[0]}
        if len(numbers) > 1 and comparative_key:
            value_cells[comparative_key] = numbers[1]
        split_block["value_cells"] = value_cells
        split_block["row_kind"] = "statement_line_item"
        split_block["row_confidence"] = max(float(split_block.get("row_confidence") or 0.0), 0.84)
        split_block["label_confidence"] = max(float(split_block.get("label_confidence") or 0.0), 0.82)
        split_block["ownership_confidence"] = max(float(split_block.get("ownership_confidence") or 0.0), 0.84)
        split_block["value_confidence"] = max(float(split_block.get("value_confidence") or 0.0), 0.82)
        diagnostics = dict(split_block.get("diagnostics") or {})
        diagnostics["source_line"] = source_line
        diagnostics["merged_statement_line_split"] = True
        diagnostics["merged_statement_segment_index"] = segment_index
        diagnostics["merged_statement_segment_count"] = len(segments)
        diagnostics["recovery_mode"] = "orchestrator_merged_statement_line_split"
        split_block["diagnostics"] = diagnostics
        result.append(split_block)
    return result


def normalized_row_from_row_block(row_block: dict[str, Any]) -> dict[str, Any]:
    row = {"line": row_block.get("label_text") or ""}
    value_cells = row_block.get("value_cells") or {}
    if isinstance(value_cells, dict):
        row.update(value_cells)
    if row_block.get("source_bbox") is not None:
        row["source_bbox"] = row_block.get("source_bbox")
    if row_block.get("stitched_from_rows"):
        row["stitched_from_rows"] = row_block.get("stitched_from_rows")
    diagnostics = row_block.get("diagnostics") or {}
    if diagnostics.get("source_line"):
        row["source_line"] = diagnostics.get("source_line")
    return row


def inline_period_columns_for_layout(
    effective_period: str,
    comparative_period: str,
) -> tuple[str | None, str | None]:
    current_year = period_year_token(effective_period)
    comparative_year = (
        period_year_token(comparative_period)
        if comparative_period
        else (current_year - 1 if current_year else None)
    )
    current_key = str(current_year) if current_year else None
    comparative_key = str(comparative_year) if comparative_year else None
    return current_key, comparative_key


def period_year_token(period: str | None) -> int | None:
    match = re.match(r"^(\d{4})Q[1-4]$", str(period or "").upper())
    return int(match.group(1)) if match else None


def inferred_columns_from_row_blocks(row_blocks: list[dict[str, Any]]) -> list[str]:
    columns = ["line"]
    seen: set[str] = set()
    for block in row_blocks:
        for key in list((block.get("value_cells") or {}).keys()):
            if key not in seen:
                seen.add(str(key))
                columns.append(str(key))
    return columns


def split_statement_line_segments(text: str, statement_type: str) -> list[tuple[str, list[str]]]:
    tokens = [token for token in str(text or "").strip().split() if token]
    if len(tokens) < 6:
        return []
    segments: list[tuple[str, list[str]]] = []
    cursor = 0
    while cursor < len(tokens):
        first_numeric_index = next(
            (index for index in range(cursor, len(tokens)) if parse_numeric_text(tokens[index]) is not None),
            None,
        )
        if first_numeric_index is None or first_numeric_index <= cursor:
            return []
        label = " ".join(tokens[cursor:first_numeric_index]).strip()
        numbers = leading_numeric_text_tokens(tokens[first_numeric_index:], limit=2)
        if not label or not numbers or not is_statement_like_label(label, statement_type):
            return []
        segments.append((label, numbers))
        cursor = first_numeric_index + len(numbers)
    return segments if len(segments) >= 2 else []


def split_statement_line_segments_from_layout_tokens(
    *,
    page_layout: dict[str, Any],
    row_index: int,
    statement_type: str,
) -> list[tuple[str, list[str]]]:
    tokens = sort_layout_tokens([token for token in list(page_layout.get("tokens") or []) if token.get("row_index") == row_index])
    if len(tokens) < 6:
        return []
    segments: list[tuple[str, list[str]]] = []
    current_tokens: list[str] = []
    previous_x0: float | None = None
    seen_numeric = False
    line_left = min((bbox_x0(token.get("bbox")) for token in tokens if bbox_x0(token.get("bbox")) is not None), default=None)
    for token in tokens:
        text = str(token.get("text") or "").strip()
        if not text:
            continue
        token_x0 = bbox_x0(token.get("bbox"))
        is_numeric = parse_numeric_text(text) is not None
        x_reset = (
            seen_numeric
            and not is_numeric
            and previous_x0 is not None
            and token_x0 is not None
            and (token_x0 + 25 < previous_x0 or (line_left is not None and token_x0 <= line_left + 30))
        )
        if x_reset and current_tokens:
            parsed = parse_statement_segment_tokens(current_tokens, statement_type)
            if not parsed:
                return []
            segments.append(parsed)
            current_tokens = [text]
            seen_numeric = False
        else:
            current_tokens.append(text)
        if is_numeric:
            seen_numeric = True
        if token_x0 is not None:
            previous_x0 = token_x0
    if current_tokens:
        parsed = parse_statement_segment_tokens(current_tokens, statement_type)
        if not parsed:
            return []
        segments.append(parsed)
    return segments if len(segments) >= 2 else []


def aligned_label_tokens_for_row_block(
    row_block: dict[str, Any],
    row_index: int,
    page_layout: dict[str, Any],
) -> list[dict[str, Any]]:
    tokens = sort_layout_tokens([token for token in list(page_layout.get("tokens") or []) if token.get("row_index") == row_index])
    if not tokens:
        tokens = sort_layout_tokens(list(page_layout.get("tokens") or []))
    row_value_bboxes = [
        bbox
        for bbox in list((row_block.get("value_bboxes") or {}).values())
        if bbox is not None and bbox_x0(bbox) is not None and bbox_y0(bbox) is not None
    ]
    if not row_value_bboxes:
        return []
    row_left = min(bbox_x0(bbox) for bbox in row_value_bboxes if bbox_x0(bbox) is not None)
    row_top = min(bbox_y0(bbox) for bbox in row_value_bboxes if bbox_y0(bbox) is not None)
    row_bottom = max(bbox_y1(bbox) for bbox in row_value_bboxes if bbox_y1(bbox) is not None)
    candidate_tokens: list[dict[str, Any]] = []
    for token in tokens:
        text = str(token.get("text") or "").strip()
        if not text or parse_numeric_text(text) is not None:
            continue
        token_x0 = bbox_x0(token.get("bbox"))
        token_top = bbox_y0(token.get("bbox"))
        token_bottom = bbox_y1(token.get("bbox"))
        if token_x0 is None or token_top is None or token_bottom is None:
            continue
        if token_x0 >= row_left - 12:
            continue
        vertical_overlap = min(row_bottom, token_bottom) - max(row_top, token_top)
        if vertical_overlap < -2:
            continue
        candidate_tokens.append(token)
    return candidate_tokens


def parse_statement_segment_tokens(tokens: list[str], statement_type: str) -> tuple[str, list[str]] | None:
    if len(tokens) < 2:
        return None
    first_numeric_index = next((index for index, token in enumerate(tokens) if parse_numeric_text(token) is not None), None)
    if first_numeric_index is None or first_numeric_index == 0:
        return None
    label = " ".join(tokens[:first_numeric_index]).strip()
    numbers = leading_numeric_text_tokens(tokens[first_numeric_index:], limit=2)
    if not label or not numbers or not is_statement_like_label(label, statement_type):
        return None
    return label, numbers


def leading_numeric_text_tokens(tokens: list[str], limit: int = 2) -> list[str]:
    values: list[str] = []
    for token in tokens:
        if parse_numeric_text(token) is None:
            if values:
                break
            continue
        values.append(token)
        if len(values) >= limit:
            break
    return values


def parse_numeric_text(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = text.strip("()").replace("\u00a0", " ")
    cleaned = re.sub(r"[^0-9,.\-\s]", "", cleaned).strip()
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned and "." not in cleaned:
        parts = cleaned.split(",")
        cleaned = "".join(parts) if all(len(part) == 3 for part in parts[1:]) else cleaned.replace(",", ".")
    cleaned = cleaned.replace(" ", "")
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return -number if negative else number


def is_statement_like_label(label: str, statement_type: str) -> bool:
    normalized = normalize_merge_label(label)
    if not normalized:
        return False
    common_markers = {
        "income_statement": {"revenue", "sales", "profit", "income", "выручка", "прибыль", "операцион"},
        "cash_flow": {"cash", "operating", "activities", "purchases", "capex", "денеж", "операцион", "приобрет"},
    }
    return any(marker in normalized for marker in common_markers.get(statement_type, set()))


def sort_layout_tokens(tokens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        tokens,
        key=lambda token: (
            bbox_y0(token.get("bbox")) if bbox_y0(token.get("bbox")) is not None else 0.0,
            bbox_x0(token.get("bbox")) if bbox_x0(token.get("bbox")) is not None else 0.0,
        ),
    )


def bbox_x0(bbox: Any) -> float | None:
    if isinstance(bbox, dict):
        value = bbox.get("x0")
        return float(value) if value is not None else None
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 1 and bbox[0] is not None:
        return float(bbox[0])
    return None


def bbox_y0(bbox: Any) -> float | None:
    if isinstance(bbox, dict):
        value = bbox.get("top")
        if value is None:
            value = bbox.get("y0")
        return float(value) if value is not None else None
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 2 and bbox[1] is not None:
        return float(bbox[1])
    return None


def bbox_y1(bbox: Any) -> float | None:
    if isinstance(bbox, dict):
        value = bbox.get("bottom")
        if value is None:
            value = bbox.get("y1")
        return float(value) if value is not None else None
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 4 and bbox[3] is not None:
        return float(bbox[3])
    return None


def merge_token_bboxes(bboxes: list[Any]) -> Any:
    bboxes = [bbox for bbox in bboxes if bbox is not None]
    if not bboxes:
        return None
    if all(isinstance(bbox, dict) for bbox in bboxes):
        return {
            "x0": min(float(bbox.get("x0", 0.0)) for bbox in bboxes),
            "x1": max(float(bbox.get("x1", bbox.get("x0", 0.0))) for bbox in bboxes),
            "top": min(float(bbox.get("top", bbox.get("y0", 0.0))) for bbox in bboxes),
            "bottom": max(float(bbox.get("bottom", bbox.get("y1", bbox.get("top", bbox.get("y0", 0.0))))) for bbox in bboxes),
        }
    list_bboxes = [bbox for bbox in bboxes if isinstance(bbox, (list, tuple)) and len(bbox) >= 4]
    if len(list_bboxes) == len(bboxes):
        return [
            min(float(bbox[0]) for bbox in list_bboxes),
            min(float(bbox[1]) for bbox in list_bboxes),
            max(float(bbox[2]) for bbox in list_bboxes),
            max(float(bbox[3]) for bbox in list_bboxes),
        ]
    return bboxes[0]


def merge_value_cells(base_value_cells: dict[str, Any], candidate_value_cells: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base_value_cells)
    for key, value in candidate_value_cells.items():
        if merged.get(key) in {None, ""} and value not in {None, ""}:
            merged[key] = value
    return merged


def row_block_has_values(row_block: dict[str, Any]) -> bool:
    value_cells = row_block.get("value_cells") or {}
    return any(value not in {None, ""} for value in value_cells.values())


def is_continuation_merge_candidate(existing_label: str, candidate_label: str) -> bool:
    existing_normalized = normalize_merge_label(existing_label)
    candidate_normalized = normalize_merge_label(candidate_label)
    if not existing_normalized or not candidate_normalized:
        return False
    if candidate_is_continuation_fragment(candidate_normalized) and (
        existing_label_is_incomplete(existing_normalized) or len(existing_normalized.split()) >= 2
    ):
        return True
    return False


def semantic_row_identity(row_block: dict[str, Any]) -> dict[str, Any]:
    label = str(row_block.get("label_text") or "").strip()
    statement_type = str(
        row_block.get("statement_family")
        or row_block.get("statement_type")
        or row_block.get("normalized_statement_family")
        or ""
    )
    return {
        "statement_type": statement_type,
        "row_kind": str(row_block.get("row_kind") or ""),
        "metric_code": semantic_identity_metric_code(label, statement_type),
        "tokens": semantic_identity_tokens(label),
        "normalized": normalize_matching_text(label),
    }


def semantic_identity_metric_code(label: str, statement_type: str) -> str | None:
    if statement_type not in {"income_statement", "balance_sheet", "cash_flow"}:
        matches = {
            metric
            for candidate_type in ("income_statement", "balance_sheet", "cash_flow")
            if (metric := try_match_metric(label, candidate_type)) is not None
        }
        return next(iter(matches)) if len(matches) == 1 else None
    return try_match_metric(label, statement_type)


def try_match_metric(label: str, statement_type: str) -> str | None:
    try:
        return match_metric(label, statement_type, degraded=True)
    except Exception:
        return None


def semantic_identity_tokens(label: str) -> tuple[str, ...]:
    stopwords = {
        "and",
        "or",
        "the",
        "for",
        "of",
        "to",
        "from",
        "on",
        "other",
        "net",
        "total",
        "и",
        "или",
        "для",
        "по",
        "от",
        "на",
        "из",
        "прочие",
        "итого",
        "нетто",
    }
    tokens = []
    for token in normalize_matching_text(label).split():
        rooted = semantic_token_root(token)
        if rooted and rooted not in stopwords and not rooted.isdigit():
            tokens.append(rooted)
    return tuple(tokens)


def semantic_token_root(token: str) -> str:
    value = str(token or "").strip()
    if len(value) > 4 and value.endswith("ies"):
        return value[:-3] + "y"
    if len(value) > 4 and value.endswith("es"):
        return value[:-2]
    if len(value) > 3 and value.endswith("s"):
        return value[:-1]
    return value


def semantic_row_identities_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if not left or not right:
        return False
    left_kind = str(left.get("row_kind") or "")
    right_kind = str(right.get("row_kind") or "")
    if left_kind and right_kind and left_kind != right_kind:
        return False
    left_type = str(left.get("statement_type") or "")
    right_type = str(right.get("statement_type") or "")
    if left_type and right_type and left_type != right_type:
        return False
    left_metric = left.get("metric_code")
    right_metric = right.get("metric_code")
    if left_metric and right_metric:
        return left_metric == right_metric
    left_tokens = set(left.get("tokens") or ())
    right_tokens = set(right.get("tokens") or ())
    if not left_tokens or not right_tokens:
        return False
    if left_tokens == right_tokens:
        return True
    overlap = left_tokens & right_tokens
    if not overlap:
        return False
    coverage = max(len(overlap) / len(left_tokens), len(overlap) / len(right_tokens))
    return coverage >= 0.75


def normalize_merge_label(value: str) -> str:
    normalized = str(value or "").strip().casefold().replace("ё", "е")
    normalized = re.sub(r"[^\w\sа-яА-Я]", " ", normalized)
    return " ".join(normalized.split())


def existing_label_is_incomplete(normalized_label: str) -> bool:
    if not normalized_label:
        return False
    trailing_tokens = {"and", "or", "from", "of", "to", "for", "on", "и", "или", "от", "по", "за", "для", "с"}
    return normalized_label.split()[-1] in trailing_tokens


def candidate_is_continuation_fragment(normalized_label: str) -> bool:
    if not normalized_label:
        return False
    first_token = normalized_label.split()[0]
    continuation_heads = {
        "and",
        "or",
        "from",
        "of",
        "to",
        "for",
        "и",
        "или",
        "от",
        "по",
        "за",
        "для",
        "с",
        "revenue",
        "revenues",
        "expense",
        "expenses",
        "cost",
        "costs",
        "tax",
        "activities",
        "assets",
        "liabilities",
    }
    return first_token in continuation_heads


def first_numeric_value(value_cells: dict[str, Any]) -> float | None:
    for value in value_cells.values():
        try:
            if value is None or str(value).strip() == "":
                continue
            return float(str(value).replace(" ", "").replace(",", "."))
        except ValueError:
            continue
    return None


def second_numeric_value(value_cells: dict[str, Any]) -> float | None:
    found = []
    for value in value_cells.values():
        try:
            if value is None or str(value).strip() == "":
                continue
            found.append(float(str(value).replace(" ", "").replace(",", ".")))
        except ValueError:
            continue
        if len(found) >= 2:
            return found[1]
    return None


def sign_hint_for_value(value: float | None) -> str | None:
    if value is None:
        return None
    return "negative" if value < 0 else "positive"


def first_warning(row_block: dict[str, Any]) -> str | None:
    warnings = list((row_block.get("diagnostics") or {}).get("warnings") or [])
    return warnings[0] if warnings else None


def table_context_conflict_reason(base_table: dict[str, Any], candidate_table: dict[str, Any]) -> str | None:
    base_family = normalized_statement_family(base_table)
    candidate_family = normalized_statement_family(candidate_table)
    if base_family and candidate_family and base_family != candidate_family:
        return "statement_family_conflict"
    base_period = str(base_table.get("effective_period") or base_table.get("period") or "")
    candidate_period = str(candidate_table.get("effective_period") or candidate_table.get("period") or "")
    if base_period and candidate_period and base_period != candidate_period:
        return "period_conflict"
    base_comparative = str(base_table.get("comparative_period") or "")
    candidate_comparative = str(candidate_table.get("comparative_period") or "")
    if base_comparative and candidate_comparative and base_comparative != candidate_comparative:
        return "period_conflict"
    return None


def normalized_statement_family(table: dict[str, Any]) -> str:
    family = str(table.get("statement_family") or table.get("statement_type") or "").strip()
    return "" if family == "unknown" else family


def merge_table_context(base_table: dict[str, Any], candidate_table: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base_table)
    for field in ("unit", "currency", "unit_multiplier", "comparative_period", "period_source", "period_confidence"):
        if merged.get(field) in {None, ""} and candidate_table.get(field) not in {None, ""}:
            merged[field] = candidate_table.get(field)
    if not merged.get("table_title") and candidate_table.get("table_title"):
        merged["table_title"] = candidate_table.get("table_title")
    if not merged.get("effective_period") and candidate_table.get("effective_period"):
        merged["effective_period"] = candidate_table.get("effective_period")
    if not merged.get("period") and candidate_table.get("period"):
        merged["period"] = candidate_table.get("period")
    merged["warnings"] = sorted({*list(base_table.get("warnings") or []), *list(candidate_table.get("warnings") or [])})
    for field in ("statement_type", "statement_family", "table_role"):
        if merged.get(field) in {None, "", "unknown"} and candidate_table.get(field) not in {None, "", "unknown"}:
            merged[field] = candidate_table.get(field)
    return merged


def collect_table_engines(*tables: dict[str, Any]) -> list[str]:
    engines: set[str] = set()
    for table in tables:
        traceability = dict(table.get("source_traceability") or {})
        for engine in list(traceability.get("source_engines_involved") or []):
            if engine:
                engines.add(str(engine))
        if traceability.get("source_engine"):
            engines.add(str(traceability.get("source_engine")))
        if table.get("source_engine"):
            engines.add(str(table.get("source_engine")))
    return sorted(engines)
