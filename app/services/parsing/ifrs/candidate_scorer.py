import re
from dataclasses import dataclass

from app.services.parsing.ifrs.concept_registry import CONCEPTS, IFRSConcept
from app.services.parsing.ifrs.issuer_overrides import IssuerOverride, get_issuer_override


@dataclass(frozen=True)
class CandidateScore:
    concept_code: str | None
    confidence_score: float
    is_strong: bool
    matched_label: str | None
    rejection_reason: str | None
    score_breakdown: dict[str, bool]
    applied_issuer_override: str | None


def score_candidate(
    raw_label: str,
    statement_context: str,
    period_type: str,
    unit_detected: bool,
    currency_detected: bool,
    source_location_exists: bool,
    ticker: str | None = None,
) -> CandidateScore:
    normalized_context = normalize_context(statement_context, ticker)
    override = get_issuer_override(ticker)
    for concept in CONCEPTS.values():
        patterns = concept.raw_label_patterns + override.additional_label_patterns.get(concept.concept_code, ())
        excluded = concept.excluded_label_patterns + override.blocked_label_patterns.get(concept.concept_code, ())
        if not any(re.search(pattern, normalize_label(raw_label)) for pattern in patterns):
            continue
        if any(re.search(pattern, normalize_label(raw_label)) for pattern in excluded):
            return rejected(concept, raw_label, "excluded_label_pattern", override)
        breakdown = {
            "label_match": True,
            "statement_context_allowed": normalized_context in concept.allowed_statement_contexts,
            "period_type_allowed": period_type in concept.period_type_allowed,
            "unit_detected": unit_detected,
            "currency_detected": currency_detected,
            "source_location_exists": source_location_exists,
        }
        is_strong = all(breakdown.values())
        return CandidateScore(
            concept.concept_code,
            0.9 if is_strong else 0.65,
            is_strong,
            raw_label,
            None if is_strong else first_failed_reason(breakdown),
            breakdown,
            override.ticker if override.ticker != "UNKNOWN" else None,
        )
    return CandidateScore(None, 0.0, False, None, "no_concept_label_match", {}, None)


def concept_for_label(raw_label: str, ticker: str | None = None) -> tuple[str | None, str | None]:
    score = score_candidate(raw_label, "unknown", "unknown", False, False, False, ticker=ticker)
    return score.matched_label, score.concept_code


def normalize_context(statement_context: str, ticker: str | None) -> str:
    override = get_issuer_override(ticker)
    return override.context_aliases.get(statement_context, statement_context)


def normalize_label(raw_label: str) -> str:
    return " ".join(raw_label.casefold().split())


def rejected(
    concept: IFRSConcept, raw_label: str, reason: str, override: IssuerOverride
) -> CandidateScore:
    return CandidateScore(
        concept.concept_code,
        0.0,
        False,
        raw_label,
        reason,
        {"label_match": True},
        override.ticker if override.ticker != "UNKNOWN" else None,
    )


def first_failed_reason(breakdown: dict[str, bool]) -> str:
    return next((key for key, value in breakdown.items() if not value), "unknown")
