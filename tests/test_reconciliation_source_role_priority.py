from app.db.models import StatementFact
from app.services.parsing.reconciliation import SourceReconciler


def _fact(metric="revenue", value=100, role="financial_statements", period_type="ytd") -> StatementFact:
    return StatementFact(
        company_id=1,
        report_document_id=1,
        period="2021Q2",
        reporting_standard="IFRS",
        statement_type="income_statement",
        metric_code=metric,
        value=value,
        currency="RUB",
        unit_multiplier=1_000_000,
        period_type=period_type,
        source_location={"source_role": role, "source_url": f"https://www.lukoil.com/{role}.pdf"},
        quality_flag="exact" if role == "financial_statements" else "low_confidence_parse",
        confidence_score=0.9 if role == "financial_statements" else 0.6,
    )


def test_financial_statements_beats_press_release_without_conflict():
    facts = SourceReconciler().reconcile(
        [_fact(value=100, role="financial_statements"), _fact(value=120, role="press_release")]
    )
    assert len(facts) == 1
    assert facts[0].value == 100
    assert facts[0].quality_flag == "exact"
    assert facts[0].source_location["secondary_source_references"]


def test_ytd_fact_does_not_conflict_with_quarter_fact():
    facts = SourceReconciler().reconcile(
        [_fact(value=600, period_type="ytd"), _fact(value=350, period_type="quarter")]
    )
    assert len(facts) == 2
    assert {fact.period_type for fact in facts} == {"ytd", "quarter"}


def test_same_metric_different_period_type_does_not_conflict():
    facts = SourceReconciler().reconcile(
        [_fact(metric="net_income", value=600, period_type="ytd"), _fact(metric="net_income", value=350, period_type="quarter")]
    )
    assert len(facts) == 2
    assert all(fact.quality_flag != "conflicting_sources" for fact in facts)


def test_conflicting_sources_only_for_same_role_and_period_type():
    reconciler = SourceReconciler()
    facts = reconciler.reconcile(
        [
            _fact(value=100, role="financial_statements", period_type="ytd"),
            _fact(value=101, role="financial_statements", period_type="ytd"),
        ]
    )
    assert len(facts) == 1
    assert facts[0].quality_flag == "conflicting_sources"
    assert reconciler.warnings
