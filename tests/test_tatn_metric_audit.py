from app.db.models import StatementFact
from app.services.metrics.metric_audit import audit_metric_rows


def _fact(code: str, value: float, period_type: str) -> StatementFact:
    return StatementFact(
        company_id=1,
        report_document_id=1,
        period="2021Q2",
        reporting_standard="IFRS",
        statement_type="income_statement",
        metric_code=code,
        value=value,
        currency="RUB",
        unit_multiplier=1_000_000,
        period_type=period_type,
        source_location={"source_role": "financial_statements"},
        quality_flag="exact",
        confidence_score=0.85,
    )


def test_current_ratio_valid_if_inputs_present_same_snapshot():
    metrics = [
        {
            "period": "2021Q2",
            "metric_code": "current_ratio",
            "value": 2.0,
            "quality_flag": "exact",
            "inputs": {"current_assets": 2, "current_liabilities": 1},
            "warnings": [],
        }
    ]
    facts = [
        _fact("current_assets", 2, "balance_sheet_snapshot"),
        _fact("current_liabilities", 1, "balance_sheet_snapshot"),
    ]

    audited = audit_metric_rows(metrics, facts)

    assert audited[0]["status"] == "valid"


def test_fcf_missing_if_capex_absent():
    metrics = [
        {
            "period": "2021Q2",
            "metric_code": "fcf",
            "value": None,
            "quality_flag": "missing",
            "inputs": {"operating_cash_flow": 100, "capex": None},
            "warnings": ["Missing input capex"],
        }
    ]
    facts = [_fact("operating_cash_flow", 100, "ytd")]

    audited = audit_metric_rows(metrics, facts)

    assert audited[0]["status"] == "missing"
    assert "capex" in audited[0]["blocking_inputs"]


def test_margin_valid_on_same_ytd_coverage():
    metrics = [
        {
            "period": "2021Q2",
            "metric_code": "net_margin",
            "value": 0.1,
            "quality_flag": "exact",
            "inputs": {"net_income": 10, "revenue": 100},
            "warnings": [],
        }
    ]
    facts = [_fact("net_income", 10, "ytd"), _fact("revenue", 100, "ytd")]

    audited = audit_metric_rows(metrics, facts)

    assert audited[0]["status"] == "valid"
