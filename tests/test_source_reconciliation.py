from app.db.models import StatementFact
from app.services.parsing.reconciliation import SourceReconciler


def fact(metric, value, source_type, quality="exact"):
    return StatementFact(
        company_id=1,
        period="2021Q1",
        reporting_standard="IFRS",
        statement_type="income_statement",
        metric_code=metric,
        value=value,
        unit_multiplier=1,
        quality_flag=quality,
        source_location={"source_type": source_type},
    )


def test_same_value_from_two_sources_merges():
    result = SourceReconciler().reconcile([
        fact("revenue", 100, "issuer_ir_manifest"),
        fact("revenue", 100, "issuer_ir"),
    ])
    assert len(result) == 1
    assert result[0].value == 100
    assert "source_references" in result[0].source_location


def test_different_values_create_conflict():
    reconciler = SourceReconciler()
    result = reconciler.reconcile([
        fact("revenue", 100, "issuer_ir_manifest"),
        fact("revenue", 120, "issuer_ir"),
    ])
    assert len(result) == 1
    assert result[0].value is None
    assert result[0].quality_flag == "conflicting_sources"
    assert reconciler.warnings


def test_fixture_never_overrides_real():
    result = SourceReconciler().reconcile([
        fact("revenue", 100, "fixture", "fixture"),
        fact("revenue", 100, "issuer_ir_manifest"),
    ])
    assert result[0].source_location["source_type"] == "issuer_ir_manifest"

