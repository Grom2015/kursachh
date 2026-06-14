from __future__ import annotations

from typing import Any


def build_normalized_page_layout(
    *,
    page_number: int | None,
    source_engine: str | None,
    page_bbox: list[Any] | None = None,
    statement_title_candidates: list[str] | None = None,
    table_regions: list[dict[str, Any]] | None = None,
    tokens: list[dict[str, Any]] | None = None,
    lines: list[dict[str, Any]] | None = None,
    layout_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "page_number": page_number,
        "page_bbox": list(page_bbox or []),
        "tokens": list(tokens or []),
        "lines": list(lines or []),
        "table_regions": list(table_regions or []),
        "statement_title_candidates": list(statement_title_candidates or []),
        "layout_diagnostics": dict(layout_diagnostics or {}),
        "source_engine": source_engine,
    }


def infer_tokens_from_row_blocks(
    row_blocks: list[dict[str, Any]],
    *,
    source_engine: str | None,
) -> list[dict[str, Any]]:
    tokens: list[dict[str, Any]] = []
    for row_index, row_block in enumerate(row_blocks):
        label_bbox = row_block.get("label_bbox")
        label_text = str(row_block.get("label_text") or "").strip()
        if label_text:
            for token_index, token in enumerate(row_block.get("label_tokens") or label_text.split()):
                tokens.append(
                    {
                        "text": str(token),
                        "bbox": label_bbox,
                        "confidence": row_block.get("label_confidence"),
                        "source_engine": row_block.get("source_engine") or source_engine,
                        "token_role": "label",
                        "row_index": row_index,
                        "token_index": token_index,
                    }
                )
        for column_name, value in (row_block.get("value_cells") or {}).items():
            value_text = str(value or "").strip()
            if not value_text:
                continue
            tokens.append(
                {
                    "text": value_text,
                    "bbox": (row_block.get("value_bboxes") or {}).get(column_name),
                    "confidence": row_block.get("value_confidence"),
                    "source_engine": row_block.get("source_engine") or source_engine,
                    "token_role": "value",
                    "row_index": row_index,
                    "column_name": str(column_name),
                }
            )
    return tokens


def infer_lines_from_row_blocks(
    row_blocks: list[dict[str, Any]],
    *,
    source_engine: str | None,
) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for row_index, row_block in enumerate(row_blocks):
        source_line = str((row_block.get("diagnostics") or {}).get("source_line") or "").strip()
        value_cells = row_block.get("value_cells") or {}
        if not source_line and not row_block.get("label_text") and not value_cells:
            continue
        if not source_line:
            fragments = [str(row_block.get("label_text") or "").strip()]
            fragments.extend(str(value or "").strip() for value in value_cells.values() if str(value or "").strip())
            source_line = " | ".join(fragment for fragment in fragments if fragment)
        lines.append(
            {
                "text": source_line,
                "bbox": row_block.get("source_bbox") or row_block.get("label_bbox"),
                "confidence": row_block.get("row_confidence"),
                "source_engine": row_block.get("source_engine") or source_engine,
                "row_index": row_index,
                "row_kind": row_block.get("row_kind"),
            }
        )
    return lines


def build_page_layout_from_candidate(
    candidate: dict[str, Any],
    *,
    source_engine: str | None,
) -> dict[str, Any]:
    page_layout = dict(candidate.get("page_layout") or {})
    row_blocks = list(candidate.get("row_blocks") or [])
    table_region = {
        "table_title": candidate.get("table_title"),
        "table_bbox": candidate.get("table_bbox") or candidate.get("source_bbox"),
        "source_table_id": candidate.get("source_table_id"),
        "statement_family": candidate.get("statement_family") or candidate.get("statement_type"),
        "column_boundaries": list(candidate.get("column_boundaries") or []),
    }
    merged_table_regions = list(page_layout.get("table_regions") or [])
    if table_region["table_title"] or table_region["table_bbox"] or table_region["source_table_id"]:
        merged_table_regions.append(table_region)
    tokens = list(page_layout.get("tokens") or [])
    if not tokens and row_blocks:
        tokens = infer_tokens_from_row_blocks(row_blocks, source_engine=source_engine)
    lines = list(page_layout.get("lines") or [])
    if not lines and row_blocks:
        lines = infer_lines_from_row_blocks(row_blocks, source_engine=source_engine)
    return build_normalized_page_layout(
        page_number=candidate.get("page_number"),
        source_engine=source_engine,
        page_bbox=page_layout.get("page_bbox") or candidate.get("page_bbox"),
        statement_title_candidates=page_layout.get("statement_title_candidates")
        or candidate.get("statement_title_candidates")
        or [candidate.get("table_title")] if candidate.get("table_title") else [],
        table_regions=merged_table_regions,
        tokens=tokens,
        lines=lines,
        layout_diagnostics=page_layout.get("layout_diagnostics") or candidate.get("layout_diagnostics"),
    )


def merge_page_layouts(page_layouts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged_by_page: dict[tuple[int | None, str | None], dict[str, Any]] = {}
    for layout in page_layouts:
        key = (layout.get("page_number"), layout.get("source_engine"))
        if key not in merged_by_page:
            merged_by_page[key] = build_normalized_page_layout(
                page_number=layout.get("page_number"),
                source_engine=layout.get("source_engine"),
                page_bbox=layout.get("page_bbox"),
                statement_title_candidates=layout.get("statement_title_candidates"),
                table_regions=layout.get("table_regions"),
                tokens=layout.get("tokens"),
                lines=layout.get("lines"),
                layout_diagnostics=layout.get("layout_diagnostics"),
            )
            continue
        current = merged_by_page[key]
        current["statement_title_candidates"] = sorted(
            {
                *[str(item) for item in current.get("statement_title_candidates") or [] if str(item).strip()],
                *[str(item) for item in layout.get("statement_title_candidates") or [] if str(item).strip()],
            }
        )
        current["table_regions"] = [*list(current.get("table_regions") or []), *list(layout.get("table_regions") or [])]
        current["tokens"] = [*list(current.get("tokens") or []), *list(layout.get("tokens") or [])]
        current["lines"] = [*list(current.get("lines") or []), *list(layout.get("lines") or [])]
        current["layout_diagnostics"] = {
            **dict(current.get("layout_diagnostics") or {}),
            **dict(layout.get("layout_diagnostics") or {}),
        }
        if not current.get("page_bbox") and layout.get("page_bbox"):
            current["page_bbox"] = list(layout.get("page_bbox") or [])
    return sorted(merged_by_page.values(), key=lambda item: (item.get("page_number") or 0, str(item.get("source_engine") or "")))
