from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isclose
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Company, ReportDocument, StatementFact
from app.services.parsing.dataframe_statement_parser import DataFrameStatementParser, StatementFactCandidate
from app.services.periods import period_in_range

ABS_TOLERANCE = 1.0
REL_TOLERANCE = 1e-6
HARD_CONFLICT_REASONS = {"value_conflict", "period_type_conflict", "unit_multiplier_conflict"}
TEXT_FALLBACK_TRUST_BUCKET = "text_table_fallback_semantic_gate"


@dataclass(frozen=True)
class FactComparisonKey:
    period: str
    metric_code: str
    period_type: str
    reporting_standard: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def compare_dataframe_facts_to_existing(
    db: Session,
    ticker: str,
    period_from: str,
    period_to: str,
    reporting_standard: str = "IFRS",
    replay_cache: bool = True,
    allow_text_fallback_semantic_gate: bool = False,
) -> dict[str, Any]:
    if not replay_cache:
        raise RuntimeError("DataFrame fact comparison supports replay-cache mode only.")
    ticker = ticker.upper()
    reporting_standard = reporting_standard.upper()
    company = db.scalar(select(Company).where(Company.ticker == ticker))
    if not company:
        raise RuntimeError(f"Company not found: {ticker}")

    parse_result = DataFrameStatementParser(
        db=db,
        allow_text_fallback_semantic_gate=allow_text_fallback_semantic_gate,
    ).parse(ticker, period_from, period_to, reporting_standard)
    dataframe_candidates = parse_result.facts
    existing_facts = load_existing_real_facts(db, company.id, period_from, period_to, reporting_standard)

    dataframe_by_key = {candidate_key(candidate): candidate for candidate in dataframe_candidates}
    existing_groups = group_existing_facts(existing_facts)
    existing_by_key = {key: select_primary_existing_fact(facts) for key, facts in existing_groups.items()}
    existing_by_base_key = {base_key(key): (key, fact) for key, fact in existing_by_key.items()}
    dataframe_by_base_key = {base_key(key): (key, fact) for key, fact in dataframe_by_key.items()}
    all_keys = sorted(
        set(dataframe_by_key) | set(existing_by_key),
        key=lambda item: (item.period, item.metric_code, item.period_type, item.reporting_standard),
    )
    consumed_dataframe_keys: set[FactComparisonKey] = set()
    consumed_existing_keys: set[FactComparisonKey] = set()

    matches: list[dict[str, Any]] = []
    missing_in_existing: list[dict[str, Any]] = []
    missing_in_dataframe: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    proposed_persist_plan: list[dict[str, Any]] = []

    for key in all_keys:
        dataframe_candidate = dataframe_by_key.get(key)
        existing_fact = existing_by_key.get(key)
        if dataframe_candidate and existing_fact:
            row, row_conflicts = compare_pair(db, key, existing_fact, dataframe_candidate)
            matches.append(row)
            conflicts.extend(row_conflicts)
            action = action_for_matched_candidate(dataframe_candidate, row_conflicts)
            reason = row_conflicts[0]["reason"] if row_conflicts else "existing_fact_matches_within_tolerance"
            proposed_persist_plan.append(persist_plan_row(key, dataframe_candidate, action, reason))
            consumed_dataframe_keys.add(key)
            consumed_existing_keys.add(key)
            continue
        if dataframe_candidate and not existing_fact and key not in consumed_dataframe_keys:
            candidate_base = base_key(key)
            if candidate_base in existing_by_base_key and existing_by_base_key[candidate_base][0] not in consumed_existing_keys:
                existing_key_with_conflict, existing_fact_with_conflict = existing_by_base_key[candidate_base]
                conflict_row = period_type_conflict_row(
                    db,
                    key,
                    dataframe_candidate,
                    existing_key_with_conflict,
                    existing_fact_with_conflict,
                )
                conflicts.append(conflict_row)
                proposed_persist_plan.append(
                    persist_plan_row(key, dataframe_candidate, "block_due_conflict", "period_type_conflict")
                )
                consumed_dataframe_keys.add(key)
                consumed_existing_keys.add(existing_key_with_conflict)
                continue
            missing_row = dataframe_candidate_row(key, dataframe_candidate)
            missing_in_existing.append(missing_row)
            proposed_persist_plan.append(
                persist_plan_row(key, dataframe_candidate, "add_missing_fact_candidate", "existing_fact_missing")
            )
            consumed_dataframe_keys.add(key)
            continue
        if existing_fact and not dataframe_candidate and key not in consumed_existing_keys:
            existing_base = base_key(key)
            if existing_base in dataframe_by_base_key and dataframe_by_base_key[existing_base][0] not in consumed_dataframe_keys:
                dataframe_key_with_conflict, dataframe_candidate_with_conflict = dataframe_by_base_key[existing_base]
                conflict_row = period_type_conflict_row(
                    db,
                    dataframe_key_with_conflict,
                    dataframe_candidate_with_conflict,
                    key,
                    existing_fact,
                )
                conflicts.append(conflict_row)
                proposed_persist_plan.append(
                    persist_plan_row(
                        dataframe_key_with_conflict,
                        dataframe_candidate_with_conflict,
                        "block_due_conflict",
                        "period_type_conflict",
                    )
                )
                consumed_dataframe_keys.add(dataframe_key_with_conflict)
                consumed_existing_keys.add(key)
                continue
            missing_in_dataframe.append(existing_fact_row(db, key, existing_fact))
            consumed_existing_keys.add(key)

    hard_conflicts = [item for item in conflicts if item["reason"] in HARD_CONFLICT_REASONS]
    dataframe_trust_counts = trust_bucket_counts(dataframe_candidates)
    eligibility = persist_eligibility(hard_conflicts, dataframe_trust_counts, len(dataframe_candidates), conflicts)
    report = {
        "company": ticker,
        "period_from": period_from,
        "period_to": period_to,
        "reporting_standard": reporting_standard,
        "validation_mode": "replay_cache",
        "dataframe_candidates_count": len(dataframe_candidates),
        "existing_facts_count": len(existing_facts),
        "matched_count": len(matches),
        "same_value_count": sum(1 for row in matches if row["same_value_within_tolerance"]),
        "missing_in_existing_count": len(missing_in_existing),
        "missing_in_dataframe_count": len(missing_in_dataframe),
        "conflict_count": len(conflicts),
        "safe_to_persist_in_this_stage": False,
        "persist_eligibility": eligibility,
        "recommended_next_action": recommended_next_action(eligibility),
        "source_priority_note": (
            "Existing issuer-specific real facts remain primary. DataFrame text fallback candidates are audit-only "
            "corroborating evidence unless a future separate gated persist stage promotes them."
        ),
        "tolerances": {"absolute": ABS_TOLERANCE, "relative": REL_TOLERANCE},
        "dataframe_trust_buckets": dataframe_trust_counts,
        "parse_status": parse_result.status,
        "matches": matches,
        "missing_in_existing": missing_in_existing,
        "missing_in_dataframe": missing_in_dataframe,
        "conflicts": conflicts,
        "proposed_persist_plan": proposed_persist_plan,
    }
    return report


def load_existing_real_facts(
    db: Session,
    company_id: int,
    period_from: str,
    period_to: str,
    reporting_standard: str,
) -> list[StatementFact]:
    facts = db.scalars(
        select(StatementFact).where(
            StatementFact.company_id == company_id,
            StatementFact.reporting_standard == reporting_standard,
        )
    ).all()
    return [fact for fact in facts if period_in_range(fact.period, period_from, period_to) and not is_fixture_fact(db, fact)]


def is_fixture_fact(db: Session, fact: StatementFact) -> bool:
    location = fact.source_location or {}
    document = db.get(ReportDocument, fact.report_document_id) if fact.report_document_id else None
    return bool(
        fact.quality_flag == "fixture"
        or (document and document.source_type == "fixture")
        or str(location.get("raw") or "").startswith("fixture:")
        or location.get("source_type") == "fixture"
    )


def group_existing_facts(facts: list[StatementFact]) -> dict[FactComparisonKey, list[StatementFact]]:
    groups: dict[FactComparisonKey, list[StatementFact]] = {}
    for fact in facts:
        groups.setdefault(existing_key(fact), []).append(fact)
    return groups


def select_primary_existing_fact(facts: list[StatementFact]) -> StatementFact:
    return sorted(
        facts,
        key=lambda fact: (
            source_priority(fact),
            fact.confidence_score or 0.0,
            fact.id or 0,
        ),
        reverse=True,
    )[0]


def compare_pair(
    db: Session,
    key: FactComparisonKey,
    existing_fact: StatementFact,
    dataframe_candidate: StatementFactCandidate,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    existing_stored = existing_fact.value
    existing_unit = float(existing_fact.unit_multiplier or 1.0)
    dataframe_unit = float(dataframe_candidate.unit_multiplier or 1.0)
    existing_economic = None if existing_stored is None else float(existing_stored) * existing_unit
    dataframe_economic = float(dataframe_candidate.value)
    abs_diff = None if existing_economic is None else abs(existing_economic - dataframe_economic)
    pct_diff = pct_difference(existing_economic, dataframe_economic)
    same_value = existing_economic is not None and isclose(
        existing_economic,
        dataframe_economic,
        rel_tol=REL_TOLERANCE,
        abs_tol=ABS_TOLERANCE,
    )
    row = {
        **key.to_dict(),
        "same_value_within_tolerance": same_value,
        "existing": existing_fact_payload(db, existing_fact),
        "dataframe": dataframe_candidate_payload(dataframe_candidate),
        "existing_stored_value": existing_stored,
        "existing_economic_value": existing_economic,
        "dataframe_economic_value": dataframe_economic,
        "abs_diff": abs_diff,
        "pct_diff": pct_diff,
    }
    conflicts: list[dict[str, Any]] = []
    if not same_value:
        conflicts.append({**row, "reason": "value_conflict"})
    if existing_fact.period_type != dataframe_candidate.period_type:
        conflicts.append({**row, "reason": "period_type_conflict"})
    if existing_unit != dataframe_unit:
        conflicts.append({**row, "reason": "unit_multiplier_conflict"})
    if source_role(db, existing_fact) != source_role_from_candidate(db, dataframe_candidate):
        row["source_role_conflict"] = True
        conflicts.append({**row, "reason": "source_role_conflict"})
    return row, conflicts


def period_type_conflict_row(
    db: Session,
    dataframe_key: FactComparisonKey,
    dataframe_candidate: StatementFactCandidate,
    existing_key_with_conflict: FactComparisonKey,
    existing_fact: StatementFact,
) -> dict[str, Any]:
    return {
        **dataframe_key.to_dict(),
        "reason": "period_type_conflict",
        "existing_period_type": existing_key_with_conflict.period_type,
        "dataframe_period_type": dataframe_key.period_type,
        "existing": existing_fact_payload(db, existing_fact),
        "dataframe": dataframe_candidate_payload(dataframe_candidate),
    }


def persist_eligibility(
    hard_conflicts: list[dict[str, Any]],
    dataframe_trust_counts: dict[str, int],
    dataframe_count: int,
    conflicts: list[dict[str, Any]],
) -> str:
    if hard_conflicts:
        return "blocked"
    if dataframe_count == 0:
        return "needs_review"
    fallback_count = dataframe_trust_counts.get(TEXT_FALLBACK_TRUST_BUCKET, 0)
    if conflicts or (dataframe_count and fallback_count > dataframe_count / 2):
        return "needs_review"
    return "eligible"


def recommended_next_action(eligibility: str) -> str:
    if eligibility == "blocked":
        return "Resolve reported conflicts before considering any separate gated persist stage."
    if eligibility == "needs_review":
        return "Review text-fallback trust bucket and run a separate gated persist/audit stage only if approved."
    return "Run separate gated persist command if needed."


def persist_plan_row(
    key: FactComparisonKey,
    candidate: StatementFactCandidate,
    action: str,
    reason: str,
) -> dict[str, Any]:
    required_gate = "separate_gated_persist_stage"
    if action == "block_due_conflict":
        required_gate = "conflict_resolution_required"
    elif trust_bucket(candidate) == TEXT_FALLBACK_TRUST_BUCKET:
        required_gate = "text_fallback_quality_gate_and_review"
    return {
        **key.to_dict(),
        "action": action,
        "reason": reason,
        "trust_bucket": trust_bucket(candidate),
        "required_gate_before_persist": required_gate,
    }


def action_for_matched_candidate(candidate: StatementFactCandidate, conflicts: list[dict[str, Any]]) -> str:
    if any(conflict["reason"] in HARD_CONFLICT_REASONS for conflict in conflicts):
        return "block_due_conflict"
    if conflicts:
        return "needs_review"
    if trust_bucket(candidate) == TEXT_FALLBACK_TRUST_BUCKET:
        return "add_as_corroborating_source"
    return "skip_existing_match"


def dataframe_candidate_row(key: FactComparisonKey, candidate: StatementFactCandidate) -> dict[str, Any]:
    return {**key.to_dict(), "dataframe": dataframe_candidate_payload(candidate)}


def existing_fact_row(db: Session, key: FactComparisonKey, fact: StatementFact) -> dict[str, Any]:
    return {**key.to_dict(), "existing": existing_fact_payload(db, fact)}


def existing_fact_payload(db: Session, fact: StatementFact) -> dict[str, Any]:
    document = db.get(ReportDocument, fact.report_document_id) if fact.report_document_id else None
    return {
        "fact_id": fact.id,
        "report_document_id": fact.report_document_id,
        "value": fact.value,
        "unit_multiplier": fact.unit_multiplier,
        "economic_value": None if fact.value is None else fact.value * fact.unit_multiplier,
        "period_type": fact.period_type,
        "quality_flag": fact.quality_flag,
        "confidence_score": fact.confidence_score,
        "source_location": fact.source_location,
        "source_role": document.source_role if document else None,
        "source_type": document.source_type if document else None,
        "source_url": document.source_url if document else None,
    }


def dataframe_candidate_payload(candidate: StatementFactCandidate) -> dict[str, Any]:
    return {
        "value": candidate.value,
        "unit_multiplier": candidate.unit_multiplier,
        "economic_value": candidate.value,
        "period_type": candidate.period_type,
        "quality_flag": candidate.quality_flag,
        "confidence_score": candidate.confidence_score,
        "source_location": candidate.source_location,
        "source_document_id": candidate.source_document_id,
        "extraction_method": candidate.extraction_method,
        "trust_bucket": trust_bucket(candidate),
        "warnings": candidate.warnings,
    }


def candidate_key(candidate: StatementFactCandidate) -> FactComparisonKey:
    return FactComparisonKey(
        period=candidate.period,
        metric_code=candidate.metric_code,
        period_type=candidate.period_type,
        reporting_standard=candidate.reporting_standard,
    )


def base_key(key: FactComparisonKey) -> tuple[str, str, str]:
    return (key.period, key.metric_code, key.reporting_standard)


def existing_key(fact: StatementFact) -> FactComparisonKey:
    return FactComparisonKey(
        period=fact.period,
        metric_code=fact.metric_code,
        period_type=fact.period_type,
        reporting_standard=fact.reporting_standard,
    )


def trust_bucket(candidate: StatementFactCandidate) -> str:
    location = candidate.source_location or {}
    return location.get("fact_source_kind") or candidate.extraction_method or "unknown"


def trust_bucket_counts(candidates: list[StatementFactCandidate]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        bucket = trust_bucket(candidate)
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def source_priority(fact: StatementFact) -> int:
    location = fact.source_location or {}
    method = location.get("extraction_method")
    if method and method not in {"dataframe_statement_parser", "text_fallback_semantic_gate"}:
        return 4
    if fact.quality_flag in {"high_confidence", "exact"}:
        return 3
    if method == "dataframe_statement_parser":
        return 2
    if method == "text_fallback_semantic_gate":
        return 1
    return 0


def source_role(db: Session, fact: StatementFact) -> str | None:
    document = db.get(ReportDocument, fact.report_document_id) if fact.report_document_id else None
    return document.source_role if document else None


def source_role_from_candidate(db: Session, candidate: StatementFactCandidate) -> str | None:
    location = candidate.source_location or {}
    if location.get("source_role"):
        return location.get("source_role")
    document = db.get(ReportDocument, candidate.source_document_id) if candidate.source_document_id else None
    return document.source_role if document else None


def pct_difference(existing: float | None, dataframe: float) -> float | None:
    if existing is None or existing == 0:
        return None
    return abs(existing - dataframe) / abs(existing)
