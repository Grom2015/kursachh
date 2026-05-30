from dataclasses import dataclass, field
from typing import Any

from app.db.models import ReportDocument, StatementFact

DEFAULT_KEY_FACTS = {
    "revenue",
    "net_income",
    "total_assets",
    "total_equity",
    "cash_and_equivalents",
    "operating_cash_flow",
    "capex",
}
FORBIDDEN_PRIMARY_SOURCE_ROLES = {"press_release", "fixture"}


@dataclass
class ParserQualityResult:
    status: str
    score: float
    canonical_facts_count: int
    high_confidence_fact_count: int
    high_confidence_fact_ratio: float
    conflicting_fact_count: int
    key_fact_coverage_ratio: float
    source_location_complete: bool
    statement_context_complete: bool
    period_type_complete: bool
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "score": self.score,
            "canonical_facts_count": self.canonical_facts_count,
            "high_confidence_fact_count": self.high_confidence_fact_count,
            "high_confidence_fact_ratio": self.high_confidence_fact_ratio,
            "conflicting_fact_count": self.conflicting_fact_count,
            "key_fact_coverage_ratio": self.key_fact_coverage_ratio,
            "source_location_complete": self.source_location_complete,
            "statement_context_complete": self.statement_context_complete,
            "period_type_complete": self.period_type_complete,
            "blockers": self.blockers,
            "warnings": self.warnings,
        }


def evaluate_parser_quality(
    facts: list[StatementFact],
    documents: list[ReportDocument] | None = None,
    periods: list[str] | None = None,
    key_facts: set[str] | None = None,
    min_canonical_facts: int = 10,
    min_high_confidence_ratio: float = 0.8,
) -> ParserQualityResult:
    documents = documents or []
    key_facts = key_facts or DEFAULT_KEY_FACTS
    blockers: list[str] = []
    warnings: list[str] = []
    canonical = [fact for fact in facts if fact.quality_flag != "low_confidence_parse"]
    high_confidence = [
        fact
        for fact in canonical
        if fact.quality_flag in {"exact", "derived"} and (fact.confidence_score is None or fact.confidence_score >= 0.7)
    ]
    conflicts = [fact for fact in facts if fact.quality_flag == "conflicting_sources"]
    ratio = len(high_confidence) / len(canonical) if canonical else 0.0
    source_locations = [fact.source_location or {} for fact in canonical]
    source_location_complete = all(bool(location) for location in source_locations) if canonical else False
    statement_context_complete = all(
        bool(location.get("statement_context") or location.get("table_title") or fact.statement_type)
        for fact, location in zip(canonical, source_locations, strict=False)
    ) if canonical else False
    period_type_complete = all(bool(fact.period_type) for fact in canonical) if canonical else False

    periods = periods or sorted({fact.period for fact in facts})
    expected = {(period, metric) for period in periods for metric in key_facts}
    found = {(fact.period, fact.metric_code) for fact in canonical if fact.metric_code in key_facts}
    key_coverage = len(found & expected) / len(expected) if expected else 0.0

    forbidden_roles = []
    for fact in canonical:
        role = (fact.source_location or {}).get("source_role")
        if role in FORBIDDEN_PRIMARY_SOURCE_ROLES:
            forbidden_roles.append(role)
    if not documents:
        blockers.append("No real source documents available.")
    if not canonical:
        blockers.append("Parser produced zero canonical facts.")
    if len(canonical) < min_canonical_facts:
        warnings.append(f"Canonical fact count below AUTO_READY threshold: {len(canonical)} < {min_canonical_facts}.")
    if ratio < min_high_confidence_ratio:
        blockers.append("High-confidence fact ratio below threshold.")
    if conflicts:
        blockers.append("Conflicting canonical facts are present.")
    if forbidden_roles:
        blockers.append("Primary financial facts include forbidden source roles.")
    if not source_location_complete:
        blockers.append("Some canonical facts lack source_location.")
    if not period_type_complete:
        blockers.append("Some canonical facts lack period_type.")
    if not statement_context_complete:
        warnings.append("Some canonical facts lack statement context metadata.")

    score = 0.0
    score += min(len(canonical) / max(min_canonical_facts, 1), 1.0) * 0.25
    score += ratio * 0.25
    score += key_coverage * 0.25
    score += (1.0 if not conflicts else 0.0) * 0.15
    score += (1.0 if source_location_complete and period_type_complete else 0.0) * 0.10
    status = "pass"
    if "Parser produced zero canonical facts." in blockers:
        status = "parser_blocked"
    elif blockers:
        status = "fail"
    elif warnings or key_coverage < 1.0 or len(canonical) < min_canonical_facts:
        status = "partial"
    return ParserQualityResult(
        status=status,
        score=round(score, 4),
        canonical_facts_count=len(canonical),
        high_confidence_fact_count=len(high_confidence),
        high_confidence_fact_ratio=round(ratio, 4),
        conflicting_fact_count=len(conflicts),
        key_fact_coverage_ratio=round(key_coverage, 4),
        source_location_complete=source_location_complete,
        statement_context_complete=statement_context_complete,
        period_type_complete=period_type_complete,
        blockers=sorted(set(blockers)),
        warnings=sorted(set(warnings)),
    )
