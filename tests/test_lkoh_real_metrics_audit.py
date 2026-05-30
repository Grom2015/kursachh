from app.db.models import StatementFact
from app.services.metrics.metric_audit import audit_metric_rows, metric_status_counts


def _fact(code, period_type="ytd", quality="exact"):
    return StatementFact(
        company_id=1,
        period="2021Q2",
        reporting_standard="IFRS",
        statement_type="income_statement",
        metric_code=code,
        value=100,
        currency="RUB",
        unit_multiplier=1_000_000,
        period_type=period_type,
        source_location={"source_url": "https://www.lukoil.com/report.pdf", "page": 5},
        quality_flag=quality,
        confidence_score=0.9 if quality == "exact" else 0.6,
    )


def test_metrics_audit_report_rows_created():
    rows = audit_metric_rows(
        [
            {
                "period": "2021Q2",
                "metric_code": "net_margin",
                "value": 0.1,
                "quality_flag": "exact",
                "formula": "net_income / revenue",
                "inputs": {"net_income": 10, "revenue": 100},
                "warnings": [],
            }
        ],
        [_fact("net_income"), _fact("revenue")],
    )
    assert rows[0]["status"] == "valid"
    assert rows[0]["source_locations"]


def test_audit_classifies_calculated_metrics():
    rows = audit_metric_rows(
        [
            {
                "period": "2021Q2",
                "metric_code": "revenue_growth",
                "value": 0.2,
                "quality_flag": "exact",
                "formula": "growth",
                "inputs": {"revenue": 100, "previous_revenue": 90},
                "warnings": [],
            }
        ],
        [_fact("revenue", "ytd")],
    )
    assert rows[0]["status"] == "questionable"


def test_valuation_metrics_stay_missing():
    rows = audit_metric_rows(
        [
            {
                "period": "2021Q2",
                "metric_code": "pe_ratio",
                "value": None,
                "quality_flag": "missing",
                "formula": "market_cap / net_income",
                "inputs": {},
                "warnings": ["market valuation input unavailable"],
            }
        ],
        [],
    )
    assert rows[0]["status"] == "missing"


def test_low_confidence_inputs_make_metric_questionable():
    rows = audit_metric_rows(
        [
            {
                "period": "2021Q2",
                "metric_code": "net_margin",
                "value": 0.1,
                "quality_flag": "exact",
                "formula": "net_income / revenue",
                "inputs": {"net_income": 10, "revenue": 100},
                "warnings": [],
            }
        ],
        [_fact("net_income", quality="low_confidence_parse"), _fact("revenue")],
    )
    assert rows[0]["status"] == "questionable"
    assert metric_status_counts(rows)["metrics_questionable_count"] == 1
