from app.db.models import ReportDocument, StatementFact
from app.services.quality.parser_quality_gate import evaluate_parser_quality


def _doc(role="financial_statements"):
    return ReportDocument(id=1, company_id=1, report_period="2021Q1", source_role=role, status="parsed")


def _fact(metric="revenue", period="2021Q1", quality="exact", role="financial_statements", location=True):
    return StatementFact(
        company_id=1,
        report_document_id=1,
        period=period,
        reporting_standard="IFRS",
        statement_type="profit_or_loss",
        metric_code=metric,
        value=100.0,
        unit_multiplier=1.0,
        period_type="ytd",
        source_location={"source_role": role, "source_url": "https://issuer/doc.pdf", "statement_context": "profit_or_loss"}
        if location
        else None,
        quality_flag=quality,
        confidence_score=0.9,
    )


def test_press_release_only_cannot_pass_parser_quality_for_auto_ready():
    facts = [_fact(role="press_release")]

    result = evaluate_parser_quality(facts, [_doc("press_release")], periods=["2021Q1"], min_canonical_facts=1)

    assert result.status == "fail"
    assert "Primary financial facts include forbidden source roles." in result.blockers


def test_zero_canonical_facts_produces_parser_blocked():
    result = evaluate_parser_quality([], [_doc()], periods=["2021Q1"])

    assert result.status == "parser_blocked"
    assert "Parser produced zero canonical facts." in result.blockers


def test_conflicting_facts_block_auto_ready():
    result = evaluate_parser_quality([_fact(quality="conflicting_sources")], [_doc()], periods=["2021Q1"], min_canonical_facts=1)

    assert result.status == "fail"
    assert "Conflicting canonical facts are present." in result.blockers


def test_missing_key_facts_produce_partial_not_ready():
    result = evaluate_parser_quality([_fact("revenue")], [_doc()], periods=["2021Q1"], min_canonical_facts=1)

    assert result.status == "partial"
    assert result.key_fact_coverage_ratio < 1.0
