from app.db.models import Company, MetricValue, ReportDocument, StatementFact
from app.services.analysis.result_builder import ResultBuilder


def _metric(code="revenue_growth", quality="missing"):
    return MetricValue(
        company_id=1,
        period="2021Q1",
        metric_code=code,
        metric_name=code,
        value=None,
        display_value=None,
        formula="x",
        inputs_json={},
        quality_flag=quality,
        warnings_json=["missing"],
    )


def test_fixture_only_quality(db_session):
    company = db_session.get(Company, 1)
    doc = ReportDocument(company_id=company.id, report_period="2021Q1", source_type="fixture")
    fact = StatementFact(
        company_id=company.id,
        period="2021Q1",
        reporting_standard="IFRS",
        statement_type="income_statement",
        metric_code="revenue",
        value=1,
        unit_multiplier=1,
        quality_flag="fixture",
        source_location={"source_type": "fixture"},
    )
    result = ResultBuilder().build(
        company, "2021Q1", "2021Q1", "IFRS", [fact], [_metric(quality="fixture")], [doc], {}, {}, [], "fixture"
    )
    assert result["data_quality"]["overall_quality"] == "fixture_only"


def test_real_partial_quality(db_session):
    company = db_session.get(Company, 1)
    doc = ReportDocument(company_id=company.id, report_period="2021Q1", source_type="issuer_ir_manifest")
    fact = StatementFact(
        company_id=company.id,
        period="2021Q1",
        reporting_standard="IFRS",
        statement_type="income_statement",
        metric_code="revenue",
        value=1,
        unit_multiplier=1,
        quality_flag="low_confidence_parse",
        source_location={"source_type": "issuer_ir_manifest"},
    )
    result = ResultBuilder().build(company, "2021Q1", "2021Q1", "IFRS", [fact], [_metric()], [doc], {}, {}, [], "real")
    assert result["data_quality"]["overall_quality"] == "partial"
    assert result["data_quality"]["low_confidence_fact_count"] == 1


def test_conflicts_quality_low(db_session):
    company = db_session.get(Company, 1)
    doc = ReportDocument(company_id=company.id, report_period="2021Q1", source_type="issuer_ir_manifest")
    fact = StatementFact(
        company_id=company.id,
        period="2021Q1",
        reporting_standard="IFRS",
        statement_type="income_statement",
        metric_code="revenue",
        value=None,
        unit_multiplier=1,
        quality_flag="conflicting_sources",
        source_location={"source_type": "issuer_ir_manifest"},
    )
    result = ResultBuilder().build(company, "2021Q1", "2021Q1", "IFRS", [fact], [_metric()], [doc], {}, {}, [], "real")
    assert result["data_quality"]["overall_quality"] == "low"
