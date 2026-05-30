from app.db.models import StatementFact
from app.services.parsing.period_semantics import derive_standalone_quarter


def _fact(period: str, value: float, months: int, period_type: str = "ytd") -> StatementFact:
    return StatementFact(
        company_id=1,
        report_document_id=1,
        period=period,
        reporting_standard="IFRS",
        statement_type="income_statement",
        metric_code="revenue",
        value=value,
        currency="RUB",
        unit_multiplier=1_000_000,
        period_type=period_type,
        source_location={"source_url": "https://www.lukoil.com/report.pdf", "ytd_months": months},
        quality_flag="exact",
        confidence_score=0.9,
    )


def test_ytd_6m_is_not_treated_as_standalone_q2():
    fact = _fact("2021Q2", 600, 6)
    assert fact.period_type == "ytd"
    assert fact.source_location["ytd_months"] == 6


def test_derived_q2_created_only_from_3m_and_6m():
    derived = derive_standalone_quarter(_fact("2021Q2", 600, 6), _fact("2021Q1", 250, 3))
    assert derived is not None
    assert derived.value == 350
    assert derived.period_type == "quarter"
    assert derived.quality_flag == "derived"
    assert len(derived.source_location["derived_from"]) == 2


def test_derived_q2_not_created_without_adjacent_ytd_months():
    assert derive_standalone_quarter(_fact("2021Q2", 600, 6), _fact("2021Q1", 250, 6)) is None
