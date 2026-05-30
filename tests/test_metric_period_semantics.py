from app.db.models import StatementFact
from app.services.metrics.metric_audit import audit_metric_rows


def _fact(period, code, value, period_type="ytd", quality="exact"):
    return StatementFact(
        company_id=1,
        period=period,
        reporting_standard="IFRS",
        statement_type="income_statement",
        metric_code=code,
        value=value,
        currency="RUB",
        unit_multiplier=1_000_000,
        period_type=period_type,
        source_location={"source_url": "https://www.lukoil.com/report.pdf"},
        quality_flag=quality,
        confidence_score=0.9 if quality == "exact" else 0.6,
    )


def _metric(period, code, value, inputs, quality="exact", warnings=None):
    return {
        "period": period,
        "metric_code": code,
        "value": value,
        "quality_flag": quality,
        "formula": "test",
        "inputs": inputs,
        "warnings": warnings or [],
    }


def test_margin_valid_on_same_ytd_coverage():
    facts = [_fact("2021Q2", "net_income", 10), _fact("2021Q2", "revenue", 100)]
    rows = audit_metric_rows([_metric("2021Q2", "net_margin", 0.1, {"net_income": 10, "revenue": 100})], facts)
    assert rows[0]["status"] == "valid"


def test_margin_invalid_on_mixed_coverage():
    facts = [_fact("2021Q2", "net_income", 10, "quarter"), _fact("2021Q2", "revenue", 100, "ytd")]
    rows = audit_metric_rows([_metric("2021Q2", "net_margin", 0.1, {"net_income": 10, "revenue": 100})], facts)
    assert rows[0]["status"] == "invalid"


def test_fcf_invalid_if_coverage_differs():
    facts = [_fact("2021Q2", "operating_cash_flow", 50, "ytd"), _fact("2021Q2", "capex", -10, "quarter")]
    rows = audit_metric_rows([_metric("2021Q2", "fcf", 60, {"operating_cash_flow": 50, "capex": -10})], facts)
    assert rows[0]["status"] == "invalid"


def test_current_ratio_requires_snapshot():
    facts = [
        _fact("2021Q2", "current_assets", 50, "ytd"),
        _fact("2021Q2", "current_liabilities", 25, "balance_sheet_snapshot"),
    ]
    rows = audit_metric_rows(
        [_metric("2021Q2", "current_ratio", 2, {"current_assets": 50, "current_liabilities": 25})],
        facts,
    )
    assert rows[0]["status"] == "invalid"


def test_debt_to_equity_requires_same_snapshot():
    facts = [
        _fact("2021Q2", "total_debt", 50, "balance_sheet_snapshot"),
        _fact("2021Q2", "total_equity", 25, "balance_sheet_snapshot"),
    ]
    rows = audit_metric_rows([_metric("2021Q2", "debt_to_equity", 2, {"total_debt": 50, "total_equity": 25})], facts)
    assert rows[0]["status"] == "valid"


def test_net_debt_to_ebitda_missing_without_ebitda():
    rows = audit_metric_rows(
        [_metric("2021Q2", "net_debt_to_ebitda", None, {"total_debt": 50, "cash_and_equivalents": 10})],
        [],
    )
    assert rows[0]["status"] == "missing"
