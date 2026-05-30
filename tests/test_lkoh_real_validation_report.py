from app.db.models import AnalysisJob, AnalysisResult, Company, ReportDocument, StatementFact
from app.tools.validate_lkoh_real_extraction import (
    KEY_FACTS,
    build_validation_report,
    save_validation_report,
)


def _company(db_session):
    return db_session.get(Company, 1)


def _doc(db_session, status="parsed", source_role="financial_statements"):
    doc = ReportDocument(
        company_id=1,
        report_period="2021Q1",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role=source_role,
        source_type="issuer_ir_manifest",
        source_url="https://www.lukoil.ru/report.pdf",
        file_name="report.pdf",
        file_hash="hash",
        status=status,
    )
    db_session.add(doc)
    db_session.flush()
    return doc


def _fact(db_session, doc, metric_code="revenue", value=100, quality="exact", confidence=0.95):
    fact = StatementFact(
        company_id=1,
        report_document_id=doc.id,
        period=doc.report_period,
        reporting_standard="IFRS",
        statement_type="income_statement",
        metric_code=metric_code,
        metric_name_original=metric_code,
        value=value,
        currency="RUB",
        unit_multiplier=1_000_000,
        period_type="quarter",
        source_location={
            "source_type": doc.source_type,
            "source_role": doc.source_role,
            "source_url": doc.source_url,
            "document_id": doc.id,
            "page": 3,
            "table": 1,
        },
        quality_flag=quality,
        confidence_score=confidence,
    )
    db_session.add(fact)
    db_session.flush()
    return fact


def _result(db_session, doc, metrics=None, overall_quality="partial"):
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
                "overall_quality": overall_quality,
                "real_data_used": True,
                "fixture_data_used": False,
            },
            "financial_analysis": {"metrics": metrics or []},
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


def test_validation_report_is_created_from_mocked_real_documents(db_session):
    doc = _doc(db_session)
    _fact(db_session, doc)
    job, result = _result(db_session, doc)
    db_session.commit()
    report = build_validation_report(db_session, _company(db_session), "2021Q1", "2021Q1", job, result)
    path = save_validation_report(report)
    assert path.exists()
    assert report["summary"]["source_document_count"] == 1
    assert report["summary"]["facts_extracted_count"] == 1
    assert report["source_role_summary"]["financial_statements"] == 1
    assert report["documents"][0]["source_role"] == "financial_statements"


def test_missing_facts_are_reported_with_warning(db_session):
    doc = _doc(db_session)
    _fact(db_session, doc, "revenue")
    job, result = _result(db_session, doc)
    db_session.commit()
    report = build_validation_report(db_session, _company(db_session), "2021Q1", "2021Q1", job, result)
    missing = [row for row in report["fact_coverage"] if row["status"] == "missing"]
    assert missing
    assert missing[0]["value"] is None
    assert missing[0]["warnings"]


def test_extracted_facts_include_traceability(db_session):
    doc = _doc(db_session)
    _fact(db_session, doc, "revenue")
    job, result = _result(db_session, doc)
    db_session.commit()
    report = build_validation_report(db_session, _company(db_session), "2021Q1", "2021Q1", job, result)
    extracted = next(row for row in report["fact_coverage"] if row["metric_code"] == "revenue")
    assert extracted["source_url"] == "https://www.lukoil.ru/report.pdf"
    assert "page 3" in extracted["source_location"]


def test_metric_coverage_missing_when_inputs_absent(db_session):
    doc = _doc(db_session)
    _fact(db_session, doc, "revenue")
    job, result = _result(db_session, doc)
    db_session.commit()
    report = build_validation_report(db_session, _company(db_session), "2021Q1", "2021Q1", job, result)
    row = next(item for item in report["metric_coverage"] if item["metric_code"] == "roe")
    assert row["quality_flag"] == "missing"
    assert row["warnings"]


def test_real_mode_never_reports_fixture_used(db_session):
    doc = _doc(db_session)
    _fact(db_session, doc, "revenue")
    job, result = _result(db_session, doc)
    db_session.commit()
    report = build_validation_report(db_session, _company(db_session), "2021Q1", "2021Q1", job, result)
    assert report["data_mode"] == "real"
    assert result.data_snapshot_json["fixture_data_used"] is False


def test_status_fail_when_no_real_documents(db_session):
    job, result = _result(db_session, None, overall_quality="unavailable")
    db_session.commit()
    report = build_validation_report(db_session, _company(db_session), "2021Q1", "2021Q1", job, result)
    assert report["status"] == "FAIL"
    assert report["summary"]["source_document_count"] == 0


def test_status_partial_when_documents_exist_but_facts_incomplete(db_session):
    doc = _doc(db_session)
    _fact(db_session, doc, "revenue")
    job, result = _result(db_session, doc)
    db_session.commit()
    report = build_validation_report(db_session, _company(db_session), "2021Q1", "2021Q1", job, result)
    assert report["status"] == "PARTIAL"
    assert report["summary"]["missing_key_fact_count"]


def test_validation_cannot_pass_with_only_press_release_sources(db_session):
    doc = _doc(db_session, source_role="press_release")
    for index, metric_code in enumerate(KEY_FACTS, start=1):
        _fact(db_session, doc, metric_code, value=index)
    job, result = _result(db_session, doc, overall_quality="high")
    db_session.commit()
    report = build_validation_report(db_session, _company(db_session), "2021Q1", "2021Q1", job, result)
    assert report["status"] == "FAIL"
    assert report["source_role_summary"]["press_release"] == 1
    assert "Only press release sources found" in " ".join(report["warnings"])


def test_validation_cannot_pass_with_zero_high_confidence_facts(db_session):
    doc = _doc(db_session)
    for index, metric_code in enumerate(KEY_FACTS, start=1):
        _fact(db_session, doc, metric_code, value=index, quality="low_confidence_parse", confidence=0.6)
    job, result = _result(db_session, doc, overall_quality="partial")
    db_session.commit()
    report = build_validation_report(db_session, _company(db_session), "2021Q1", "2021Q1", job, result)
    assert report["status"] == "FAIL"
    assert report["summary"]["high_confidence_fact_count"] == 0


def test_validation_can_be_partial_with_financial_statements_high_confidence_facts(db_session):
    doc = _doc(db_session)
    _fact(db_session, doc, "revenue", value=100, quality="exact", confidence=0.9)
    job, result = _result(db_session, doc, overall_quality="partial")
    db_session.commit()
    report = build_validation_report(db_session, _company(db_session), "2021Q1", "2021Q1", job, result)
    assert report["status"] == "PARTIAL"
    assert report["summary"]["facts_from_financial_statements"] == 1
    assert report["summary"]["canonical_facts_count"] == 1


def test_status_pass_only_when_minimal_real_facts_and_traceability_exist(db_session):
    doc = _doc(db_session)
    for index, metric_code in enumerate(KEY_FACTS, start=1):
        _fact(db_session, doc, metric_code, value=index)
    metrics = [
        {
            "period": "2021Q1",
            "metric_code": "ebitda_margin",
            "value": 0.2,
            "quality_flag": "exact",
            "inputs": {"ebitda": 1, "revenue": 5},
            "formula": "ebitda / revenue",
            "warnings": [],
        }
    ]
    job, result = _result(db_session, doc, metrics=metrics, overall_quality="high")
    db_session.commit()
    report = build_validation_report(db_session, _company(db_session), "2021Q1", "2021Q1", job, result)
    assert report["status"] == "PASS"
    assert report["summary"]["facts_extracted_count"] == len(KEY_FACTS)
