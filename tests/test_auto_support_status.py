from app.db.models import Company, MetricValue, ReportDocument, StatementFact
from app.services.quality.auto_support_status import evaluate_auto_support_status


def _doc(company):
    return ReportDocument(
        id=1,
        company=company,
        company_id=1,
        report_period="2021Q1",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="issuer_ir_manifest",
        source_url="https://trusted.example/doc.pdf",
        storage_path="missing.pdf",
        file_name="doc.pdf",
        file_hash="hash",
        status="parsed",
    )


def _fact(metric):
    return StatementFact(
        company_id=1,
        report_document_id=1,
        period="2021Q1",
        reporting_standard="IFRS",
        statement_type="profit_or_loss",
        metric_code=metric,
        value=100,
        unit_multiplier=1,
        period_type="ytd",
        source_location={"source_role": "financial_statements", "statement_context": "profit_or_loss"},
        quality_flag="exact",
        confidence_score=0.9,
    )


def _metric(code="net_margin", value=0.1):
    return MetricValue(
        company_id=1,
        period="2021Q1",
        metric_code=code,
        metric_name=code,
        value=value,
        display_value=str(value),
        formula="test",
        inputs_json={},
        quality_flag="exact",
        warnings_json=[],
    )


def test_source_blocked_when_no_documents():
    result = evaluate_auto_support_status("TEST", "2021Q1", "2021Q1", "IFRS", "NOT_READY", [], [], [])

    assert result.status == "SOURCE_BLOCKED"
    assert result.manual_action_required is False


def test_parser_blocked_when_documents_exist_but_no_facts():
    company = Company(ticker="TEST", board="TQBR", short_name="PJSC Test", full_name="PJSC Test")
    result = evaluate_auto_support_status("TEST", "2021Q1", "2021Q1", "IFRS", "READY", [_doc(company)], [], [])

    assert result.status in {"SOURCE_BLOCKED", "PARSER_BLOCKED"}
    assert result.manual_action_required is False


def test_manual_pass_is_metadata_not_required_for_auto_partial():
    facts = [_fact(metric) for metric in ["revenue", "net_income", "total_assets"]]
    result = evaluate_auto_support_status(
        "TEST",
        "2021Q1",
        "2021Q1",
        "IFRS",
        "READY",
        [],
        facts,
        [_metric()],
        manual_verification={"status": "pass"},
    )

    assert result.manual_action_required is False
    assert result.status != "AUTO_READY"


def test_manual_fail_blocks_auto_ready():
    facts = [_fact(metric) for metric in ["revenue", "net_income", "total_assets"]]
    result = evaluate_auto_support_status(
        "TEST",
        "2021Q1",
        "2021Q1",
        "IFRS",
        "READY",
        [],
        facts,
        [_metric()],
        manual_verification={"status": "fail"},
    )

    assert result.status != "AUTO_READY"
    assert "Manual QA metadata reports failed checks." in result.blockers
