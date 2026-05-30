from app.db.models import AnalysisJob, AnalysisResult, Company, ReportDocument, StatementFact
from app.services.validation import lkoh_manual_verification as manual
from app.tools.validate_lkoh_real_extraction import build_validation_report


def _doc(db_session):
    doc = ReportDocument(
        company_id=1,
        report_period="2021Q1",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="issuer_ir_manifest",
        source_url="https://www.lukoil.com/FileSystem/9/report.pdf",
        file_name="report.pdf",
        file_hash="hash",
        status="parsed",
    )
    db_session.add(doc)
    db_session.flush()
    return doc


def _fact(
    db_session,
    doc,
    metric_code="revenue",
    value=100.0,
    period_type="ytd",
    statement_type="income_statement",
    source_location=None,
    unit_multiplier=1_000_000,
):
    location = source_location if source_location is not None else {
        "source_type": doc.source_type,
        "source_role": doc.source_role,
        "source_url": doc.source_url,
        "document_id": doc.id,
        "page": 5,
        "table": "statement_text",
        "line": 9,
        "raw_label": metric_code,
    }
    fact = StatementFact(
        company_id=1,
        report_document_id=doc.id,
        period=doc.report_period,
        reporting_standard="IFRS",
        statement_type=statement_type,
        metric_code=metric_code,
        metric_name_original=metric_code,
        value=value,
        currency="RUB",
        unit_multiplier=unit_multiplier,
        period_type=period_type,
        source_location=location,
        quality_flag="exact",
        confidence_score=0.9,
    )
    db_session.add(fact)
    db_session.flush()
    return fact


def _result(db_session, doc):
    job = AnalysisJob(
        company_query="LKOH",
        period_from="2021Q1",
        period_to="2021Q1",
        reporting_standard="IFRS",
        data_mode="real",
        status="succeeded",
    )
    db_session.add(job)
    db_session.flush()
    result = AnalysisResult(
        job_id=job.id,
        company_id=1,
        period_from="2021Q1",
        period_to="2021Q1",
        reporting_standard="IFRS",
        data_snapshot_json={"data_mode": "real", "real_data_used": True, "fixture_data_used": False},
        result_json={
            "data_quality": {
                "data_mode": "real",
                "overall_quality": "partial",
                "real_data_used": True,
                "fixture_data_used": False,
            },
            "source_documents": [{"id": doc.id, "source_type": doc.source_type}],
            "financial_analysis": {"metrics": []},
            "warnings": [],
        },
        llm_payload_json={},
        warnings_json=[],
        disclaimer="test",
    )
    db_session.add(result)
    db_session.flush()
    job.result_id = result.id
    return job, result


def _golden(checks):
    return {
        "company": "LKOH",
        "period_from": "2021Q1",
        "period_to": "2021Q4",
        "reporting_standard": "IFRS",
        "checks": checks,
    }


def _check(index=0, value=100.0, status="verified", unit_multiplier=1_000_000):
    return {
        "period": "2021Q1",
        "metric_code": f"metric_{index}" if index else "revenue",
        "expected_value": value,
        "expected_currency": "RUB",
        "expected_unit_multiplier": unit_multiplier,
        "expected_period_type": "ytd",
        "expected_source_role": "financial_statements",
        "expected_source_location_contains": ["page", "table"],
        "manual_status": status,
        "reviewer_notes": "",
    }


def test_manual_review_pack_generated_from_mocked_facts(db_session):
    doc = _doc(db_session)
    _fact(db_session, doc, "revenue", statement_type="income_statement", period_type="ytd")
    _fact(db_session, doc, "total_assets", statement_type="balance_sheet", period_type="balance_sheet_snapshot")
    _fact(db_session, doc, "operating_cash_flow", statement_type="cash_flow", period_type="ytd")
    _fact(db_session, doc, "total_debt", statement_type="balance_sheet", period_type="balance_sheet_snapshot")
    _result(db_session, doc)
    db_session.commit()
    pack = manual.generate_manual_review_pack(
        "2021Q1",
        "2021Q1",
        limit=20,
        db=db_session,
        output_path=manual.golden_dir() / "test_tmp" / "manual_pack_generated.json",
    )
    assert pack["facts_selected"] == 4
    assert {"revenue", "total_assets", "operating_cash_flow", "total_debt"} <= set(pack["metric_codes_covered"])


def test_review_pack_includes_source_location_and_raw_label(db_session):
    doc = _doc(db_session)
    _fact(db_session, doc, "revenue")
    _result(db_session, doc)
    db_session.commit()
    pack = manual.generate_manual_review_pack(
        "2021Q1",
        "2021Q1",
        limit=1,
        db=db_session,
        output_path=manual.golden_dir() / "test_tmp" / "manual_pack_traceability.json",
    )
    item = pack["items"][0]
    assert "page 5" in item["source_location"]
    assert item["raw_label"] == "revenue"


def test_golden_verification_no_verified_checks_when_all_pending(monkeypatch):
    monkeypatch.setattr(manual, "load_golden_checks", lambda path=None: _golden([_check(status="pending")]))
    report = manual.verify_golden_checks("2021Q1", "2021Q1", facts=[], persist=False)
    assert report["status"] == "NO_VERIFIED_CHECKS"
    assert report["pending_count"] == 1


def test_golden_verification_pass_when_twenty_verified_checks_match(db_session, monkeypatch):
    doc = _doc(db_session)
    checks = []
    facts = []
    for index in range(20):
        code = "revenue" if index == 0 else f"metric_{index}"
        checks.append(_check(index=index, value=100 + index))
        facts.append(_fact(db_session, doc, code, value=100 + index))
    monkeypatch.setattr(manual, "load_golden_checks", lambda path=None: _golden(checks))
    report = manual.verify_golden_checks("2021Q1", "2021Q1", facts=facts, persist=False)
    assert report["status"] == "PASS"
    assert report["passed_count"] == 20
    assert report["accuracy"] == 1.0


def test_golden_verification_fail_when_expected_value_differs(db_session, monkeypatch):
    doc = _doc(db_session)
    fact = _fact(db_session, doc, "revenue", value=100)
    monkeypatch.setattr(manual, "load_golden_checks", lambda path=None: _golden([_check(value=200)]))
    report = manual.verify_golden_checks("2021Q1", "2021Q1", facts=[fact], persist=False)
    assert report["status"] == "FAIL"
    assert report["failed_count"] == 1


def test_unit_multiplier_mismatch_fails(db_session, monkeypatch):
    doc = _doc(db_session)
    fact = _fact(db_session, doc, "revenue", value=100, unit_multiplier=1_000)
    monkeypatch.setattr(manual, "load_golden_checks", lambda path=None: _golden([_check(value=100)]))
    report = manual.verify_golden_checks("2021Q1", "2021Q1", facts=[fact], persist=False)
    assert report["failed_count"] == 1
    assert "unit_multiplier mismatch" in report["failures"][0]["reason"]


def test_missing_source_location_fails(db_session, monkeypatch):
    doc = _doc(db_session)
    fact = _fact(db_session, doc, "revenue", value=100, source_location={"source_role": "financial_statements"})
    monkeypatch.setattr(manual, "load_golden_checks", lambda path=None: _golden([_check(value=100)]))
    report = manual.verify_golden_checks("2021Q1", "2021Q1", facts=[fact], persist=False)
    assert report["failed_count"] == 1
    assert "source_location" in report["failures"][0]["reason"]


def test_validation_report_includes_manual_verification_section(db_session, monkeypatch):
    doc = _doc(db_session)
    _fact(db_session, doc, "revenue")
    job, result = _result(db_session, doc)
    db_session.commit()
    monkeypatch.setattr(manual, "load_golden_checks", lambda path=None: _golden([_check(status="pending")]))
    report = build_validation_report(db_session, db_session.get(Company, 1), "2021Q1", "2021Q1", job, result)
    assert report["manual_verification"]["status"] == "in_progress"
    assert report["manual_verification"]["pending_count"] == 1
