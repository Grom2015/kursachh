from collections import defaultdict

from app.db.models import StatementFact

SOURCE_PRIORITY = {
    "manual_verified": 100,
    "issuer_ir_manifest": 70,
    "issuer_ir": 70,
    "fixture": 10,
}

SOURCE_ROLE_PRIORITY = {
    "manual_verified": 100,
    "financial_statements": 80,
    "financial_supplement": 70,
    "annual_report": 60,
    "press_release": 30,
    "fixture": 10,
}


class SourceReconciler:
    def __init__(self, tolerance: float = 1e-6):
        self.tolerance = tolerance
        self.warnings: list[str] = []

    def reconcile(self, facts: list[StatementFact]) -> list[StatementFact]:
        grouped: dict[tuple, list[StatementFact]] = defaultdict(list)
        for fact in facts:
            grouped[
                (
                    fact.company_id,
                    fact.period,
                    fact.reporting_standard,
                    fact.metric_code,
                    fact.period_type,
                )
            ].append(fact)

        canonical: list[StatementFact] = []
        for key, candidates in grouped.items():
            if len(candidates) == 1:
                canonical.append(candidates[0])
                continue
            candidates = sorted(candidates, key=self._priority, reverse=True)
            top_priority = self._priority(candidates[0])
            comparable = [candidate for candidate in candidates if self._priority(candidate) == top_priority]
            secondary = [candidate for candidate in candidates if self._priority(candidate) < top_priority]
            values = [candidate.value for candidate in comparable if candidate.value is not None]
            if values and all(abs(value - values[0]) <= self.tolerance for value in values):
                chosen = comparable[0]
                chosen.source_location = {
                    **(chosen.source_location or {}),
                    "source_references": [candidate.source_location for candidate in comparable],
                    "secondary_source_references": [candidate.source_location for candidate in secondary],
                }
                canonical.append(chosen)
                continue
            if secondary and len(comparable) == 1:
                chosen = comparable[0]
                chosen.source_location = {
                    **(chosen.source_location or {}),
                    "secondary_source_references": [candidate.source_location for candidate in secondary],
                }
                canonical.append(chosen)
                continue
            chosen = comparable[0]
            chosen.value = None
            chosen.quality_flag = "conflicting_sources"
            chosen.confidence_score = min(chosen.confidence_score or 0.5, 0.5)
            chosen.source_location = {
                **(chosen.source_location or {}),
                "conflicting_values": [
                    {
                        "value": candidate.value,
                        "quality_flag": candidate.quality_flag,
                        "source_location": candidate.source_location,
                    }
                    for candidate in comparable
                ],
                "secondary_source_references": [candidate.source_location for candidate in secondary],
            }
            self.warnings.append(f"Conflicting sources for {key[3]} {key[1]} {key[4]}; canonical value withheld")
            canonical.append(chosen)
        return canonical

    def _priority(self, fact: StatementFact) -> int:
        location = fact.source_location or {}
        source_role = location.get("source_role")
        if not source_role and fact.quality_flag == "fixture":
            source_role = "fixture"
        if source_role:
            return SOURCE_ROLE_PRIORITY.get(str(source_role), 50)
        source_type = location.get("source_type")
        return SOURCE_PRIORITY.get(str(source_type), 50)
