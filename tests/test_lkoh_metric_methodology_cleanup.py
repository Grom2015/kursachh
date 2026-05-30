from app.db.models import StatementFact
from app.services.metrics.metric_audit import (
    audit_metric_rows,
    metric_decision_table,
    missing_metrics_breakdown,
)


def _fact(code, value=100.0, period="2021Q2", period_type="ytd", quality="exact"):
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
        source_location={
            "source_role": "financial_statements",
            "source_url": "https://www.lukoil.com/report.pdf",
            "page": 5,
            "table": "statement_text",
        },
        quality_flag=quality,
        confidence_score=0.9,
    )


def _metric(code, value=None, quality="missing", inputs=None, warnings=None):
    return {
        "period": "2021Q2",
        "metric_code": code,
        "value": value,
        "quality_flag": quality,
        "formula": "formula",
        "inputs": inputs or {},
        "warnings": warnings or ([] if value is not None else ["missing input"]),
    }


def test_audit_classifies_ebitda_metrics_as_requires_explicit_disclosure():
    rows = audit_metric_rows(
        [_metric("ebitda_margin", inputs={"ebitda": None, "revenue": 100})],
        [_fact("revenue")],
    )
    assert rows[0]["missing_reason"] == "missing_ebitda"
    grouped = missing_metrics_breakdown(rows)
    assert grouped["requires_explicit_disclosure"][0]["metric_code"] == "ebitda_margin"


def test_audit_classifies_pe_ratio_as_requires_market_inputs():
    rows = audit_metric_rows([_metric("pe_ratio", inputs={})], [])
    assert rows[0]["missing_reason"] == "missing_market_cap"
    grouped = missing_metrics_breakdown(rows)
    assert grouped["requires_market_inputs"][0]["metric_code"] == "pe_ratio"


def test_current_ratio_valid_when_snapshot_inputs_exist():
    rows = audit_metric_rows(
        [_metric("current_ratio", value=2.0, quality="exact", inputs={"current_assets": 200, "current_liabilities": 100})],
        [
            _fact("current_assets", 200, period_type="balance_sheet_snapshot"),
            _fact("current_liabilities", 100, period_type="balance_sheet_snapshot"),
        ],
    )
    assert rows[0]["status"] == "valid"


def test_debt_to_equity_usable_with_warning_when_total_debt_derived():
    rows = audit_metric_rows(
        [_metric("debt_to_equity", value=0.5, quality="exact", inputs={"total_debt": 50, "total_equity": 100})],
        [
            _fact("total_debt", 50, period_type="balance_sheet_snapshot", quality="derived"),
            _fact("total_equity", 100, period_type="balance_sheet_snapshot"),
        ],
    )
    table = metric_decision_table(rows)
    assert rows[0]["status"] == "valid"
    assert table[0]["decision"] == "usable_with_warning"


def test_fcf_valid_when_ocf_and_capex_same_ytd_coverage():
    rows = audit_metric_rows(
        [_metric("fcf", value=80, quality="exact", inputs={"operating_cash_flow": 100, "capex": -20})],
        [_fact("operating_cash_flow", 100), _fact("capex", -20)],
    )
    assert rows[0]["status"] == "valid"


def test_margin_valid_when_inputs_have_same_ytd_coverage():
    rows = audit_metric_rows(
        [_metric("net_margin", value=0.1, quality="exact", inputs={"net_income": 10, "revenue": 100})],
        [_fact("net_income", 10), _fact("revenue", 100)],
    )
    assert rows[0]["status"] == "valid"


def test_roe_questionable_if_average_equity_unavailable():
    rows = audit_metric_rows(
        [
            _metric(
                "roe",
                value=0.2,
                quality="derived",
                inputs={"net_income": 20, "total_equity": 100, "net_income_ttm": 20, "average_equity": 100},
                warnings=["Average equity unavailable; current equity used"],
            )
        ],
        [_fact("net_income", 20, period_type="annual"), _fact("total_equity", 100, period_type="balance_sheet_snapshot")],
    )
    assert rows[0]["status"] == "questionable"
    assert rows[0]["questionable_reason"] in {
        "average_balance_sheet_unavailable",
        "ytd_income_with_snapshot_balance_sheet",
    }


def test_missing_metrics_breakdown_groups_reasons_correctly():
    rows = audit_metric_rows(
        [
            _metric("ebitda_margin", inputs={"ebitda": None, "revenue": 100}),
            _metric("pe_ratio", inputs={}),
            _metric("revenue_growth", inputs={"revenue": 100, "previous_revenue": None}),
        ],
        [_fact("revenue")],
    )
    grouped = missing_metrics_breakdown(rows)
    assert len(grouped["requires_explicit_disclosure"]) == 1
    assert len(grouped["requires_market_inputs"]) == 1
    assert len(grouped["blocked_by_period_semantics"]) == 1


def test_metric_decision_table_is_generated():
    rows = audit_metric_rows(
        [_metric("current_ratio", value=2.0, quality="exact", inputs={"current_assets": 200, "current_liabilities": 100})],
        [
            _fact("current_assets", 200, period_type="balance_sheet_snapshot"),
            _fact("current_liabilities", 100, period_type="balance_sheet_snapshot"),
        ],
    )
    table = metric_decision_table(rows)
    assert table[0]["metric_code"] == "current_ratio"
    assert table[0]["decision"] == "usable"


def test_readme_status_does_not_claim_production_ready():
    text = open("README.md", encoding="utf-8").read()
    assert "not production-ready" in text
