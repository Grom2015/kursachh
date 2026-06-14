from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class RowCandidate:
    source_engine: str
    source_page: int | None
    source_table_id: str | None
    source_bbox: Any = None
    label_text: str | None = None
    current_value: float | None = None
    comparative_value: float | None = None
    effective_period: str | None = None
    comparative_period: str | None = None
    statement_family: str | None = None
    row_kind: str | None = None
    sign_hint: str | None = None
    confidence: float = 0.0
    lower_trust_warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FusionDecision:
    allowed: bool
    fusion_status: str
    source_engines_involved: list[str]
    reason: str | None = None
    warning: str | None = None
    merged_candidate: RowCandidate | None = None
    conflict_entry: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.merged_candidate is not None:
            payload["merged_candidate"] = self.merged_candidate.to_dict()
        return payload


def fuse_row_candidates(
    candidates: list[RowCandidate],
    *,
    engine_priority: dict[str, int] | None = None,
) -> FusionDecision:
    if not candidates:
        return FusionDecision(
            allowed=False,
            fusion_status="conflict_retained_as_evidence",
            source_engines_involved=[],
            reason="no_candidates",
        )
    if len(candidates) == 1:
        candidate = candidates[0]
        warning = candidate.lower_trust_warning or ("ocr_only_lower_trust" if "ocr" in candidate.source_engine else None)
        return FusionDecision(
            allowed=True,
            fusion_status="single_engine",
            source_engines_involved=[candidate.source_engine],
            warning=warning,
            merged_candidate=candidate,
        )

    priorities = engine_priority or {}
    sorted_candidates = sorted(
        candidates,
        key=lambda item: (
            priorities.get(item.source_engine, 0),
            item.confidence,
        ),
        reverse=True,
    )
    primary, secondary = sorted_candidates[0], sorted_candidates[1]
    conflict_reason = _conflict_reason(primary, secondary)
    if conflict_reason:
        return FusionDecision(
            allowed=False,
            fusion_status="conflict_retained_as_evidence",
            source_engines_involved=_engines(candidates),
            reason=conflict_reason,
            conflict_entry={"conflict_reason": conflict_reason, "candidates": [item.to_dict() for item in candidates]},
        )
    if not _eligible_for_merge(primary, secondary):
        return FusionDecision(
            allowed=False,
            fusion_status="conflict_retained_as_evidence",
            source_engines_involved=_engines(candidates),
            reason="weak_alignment",
            conflict_entry={"conflict_reason": "weak_alignment", "candidates": [item.to_dict() for item in candidates]},
        )
    merged = RowCandidate(
        source_engine=primary.source_engine,
        source_page=primary.source_page,
        source_table_id=primary.source_table_id,
        source_bbox=primary.source_bbox or secondary.source_bbox,
        label_text=primary.label_text or secondary.label_text,
        current_value=primary.current_value if primary.current_value is not None else secondary.current_value,
        comparative_value=(
            primary.comparative_value if primary.comparative_value is not None else secondary.comparative_value
        ),
        effective_period=primary.effective_period or secondary.effective_period,
        comparative_period=primary.comparative_period or secondary.comparative_period,
        statement_family=primary.statement_family or secondary.statement_family,
        row_kind=primary.row_kind or secondary.row_kind,
        sign_hint=primary.sign_hint or secondary.sign_hint,
        confidence=max(primary.confidence, secondary.confidence),
        lower_trust_warning=_merged_lower_trust_warning(candidates),
    )
    return FusionDecision(
        allowed=True,
        fusion_status="merged_engines",
        source_engines_involved=_engines(candidates),
        warning=merged.lower_trust_warning,
        merged_candidate=merged,
    )


def _conflict_reason(left: RowCandidate, right: RowCandidate) -> str | None:
    if left.statement_family and right.statement_family and left.statement_family != right.statement_family:
        return "statement_family_conflict"
    if left.effective_period and right.effective_period and left.effective_period != right.effective_period:
        return "period_conflict"
    if left.comparative_period and right.comparative_period and left.comparative_period != right.comparative_period:
        return "period_conflict"
    if left.sign_hint and right.sign_hint and left.sign_hint != right.sign_hint:
        return "sign_conflict"
    if left.source_page is not None and right.source_page is not None and left.source_page != right.source_page:
        return "page_relation_conflict"
    if left.row_kind and right.row_kind and left.row_kind != right.row_kind:
        return "row_alignment_conflict"
    return None


def _eligible_for_merge(left: RowCandidate, right: RowCandidate) -> bool:
    if left.row_kind and left.row_kind not in {"statement_line_item", "subtotal", "grand_total"}:
        return False
    if right.row_kind and right.row_kind not in {"statement_line_item", "subtotal", "grand_total"}:
        return False
    if not _labels_compatible(left.label_text, right.label_text):
        return False
    if left.label_text and right.label_text and left.label_text != right.label_text:
        return False
    if left.current_value is not None and right.current_value is not None and left.current_value != right.current_value:
        return False
    if (
        left.comparative_value is not None
        and right.comparative_value is not None
        and left.comparative_value != right.comparative_value
    ):
        return False
    return bool((left.label_text or right.label_text) and (left.current_value is not None or right.current_value is not None))


def _labels_compatible(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return True
    return left.strip().lower() == right.strip().lower()


def _engines(candidates: list[RowCandidate]) -> list[str]:
    return sorted({candidate.source_engine for candidate in candidates})


def _merged_lower_trust_warning(candidates: list[RowCandidate]) -> str | None:
    engines = _engines(candidates)
    if all("ocr" in engine for engine in engines):
        return "ocr_only_lower_trust"
    if any("ocr" in engine for engine in engines):
        return None
    return None
