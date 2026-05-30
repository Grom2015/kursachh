from pathlib import Path
from uuid import uuid4

import httpx
import yaml

from app.core.config import get_settings
from app.db.models import AnalysisJob, AnalysisResult, Company, ReportDocument, StatementFact
from app.tools.validate_lkoh_real_extraction import KEY_FACTS, build_validation_report
from app.tools.verify_lkoh_source_package import SourcePackageVerifier


def _write_manifest(root: Path, reports: list[dict]) -> None:
    path = root / "data" / "manifests"
    path.mkdir(parents=True)
    (path / "lkoh_real_sources.yml").write_text(
        yaml.safe_dump({"company": "LKOH", "source_type": "issuer_ir_manifest", "reports": reports}, sort_keys=False),
        encoding="utf-8",
    )


def _test_root() -> Path:
    root = Path("data") / "validation" / "test_tmp" / f"lkoh-source-package-{uuid4()}"
    root.mkdir(parents=True)
    return root


def _item(period: str, role: str, url: str | None = None, expected_file_type: str = "pdf") -> dict:
    return {
        "period": period,
        "reporting_standard": "IFRS",
        "document_type": role,
        "source_role": role,
        "title": f"{period} {role}",
        "source_url": url or f"https://www.lukoil.com/{period}-{role}.pdf",
        "expected_file_type": expected_file_type,
        "language": "en",
    }


def test_source_package_all_press_release_only_not_ready():
    get_settings().report_source_allowed_domains = ["www.lukoil.com"]
    root = _test_root()
    _write_manifest(root, [_item(period, "press_release") for period in ["2021Q1", "2021Q2"]])
    report = SourcePackageVerifier(root).verify("2021Q1", "2021Q2")
    assert report["status"] == "NOT_READY"
    assert report["summary"]["periods_only_press_release"] == 2
    assert report["warnings"]


def test_source_package_every_period_has_financial_statements_ready():
    get_settings().report_source_allowed_domains = ["www.lukoil.com"]
    root = _test_root()
    _write_manifest(root, [_item(period, "financial_statements") for period in ["2021Q1", "2021Q2"]])
    report = SourcePackageVerifier(root).verify("2021Q1", "2021Q2")
    assert report["status"] == "READY"
    assert report["summary"]["periods_with_financial_statements"] == 2


def test_source_package_mixed_sources_partial():
    get_settings().report_source_allowed_domains = ["www.lukoil.com"]
    root = _test_root()
    _write_manifest(
        root,
        [_item("2021Q1", "financial_supplement", expected_file_type="xlsx"), _item("2021Q2", "press_release")],
    )
    report = SourcePackageVerifier(root).verify("2021Q1", "2021Q2")
    assert report["status"] == "PARTIAL"
    assert report["summary"]["periods_with_financial_supplement"] == 1
    assert report["summary"]["periods_only_press_release"] == 1


def test_source_package_invalid_expected_file_type_warning():
    get_settings().report_source_allowed_domains = ["www.lukoil.com"]
    root = _test_root()
    _write_manifest(root, [_item("2021Q1", "financial_statements", expected_file_type="doc")])
    report = SourcePackageVerifier(root).verify("2021Q1", "2021Q1")
    source = report["periods"][0]["sources"][0]
    assert "Invalid expected_file_type" in " ".join(source["warnings"])


def test_source_package_non_allowlisted_domain_warning():
    get_settings().report_source_allowed_domains = ["www.lukoil.com"]
    root = _test_root()
    _write_manifest(root, [_item("2021Q1", "financial_statements", url="https://evil.example/report.pdf")])
    report = SourcePackageVerifier(root).verify("2021Q1", "2021Q1")
    source = report["periods"][0]["sources"][0]
    assert source["trusted_domain"] is False
    assert "allowlisted" in " ".join(source["warnings"])


def test_live_metadata_falls_back_to_get_when_head_fails(monkeypatch):
    get_settings().report_source_allowed_domains = ["www.lukoil.com"]
    root = _test_root()
    _write_manifest(root, [_item("2021Q1", "financial_statements")])

    class FakeResponse:
        def __init__(self, status_code, content_type):
            self.status_code = status_code
            self.headers = {"content-type": content_type}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("error", request=None, response=None)

    class FakeClient:
        def __init__(self, timeout, follow_redirects, verify=True):
            self.timeout = timeout
            self.follow_redirects = follow_redirects
            self.verify = verify

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def head(self, url):
            return FakeResponse(404, "text/html")

        def get(self, url):
            return FakeResponse(200, "application/pdf")

    monkeypatch.setattr(httpx, "Client", FakeClient)
    report = SourcePackageVerifier(root).verify("2021Q1", "2021Q1", live=True)
    source = report["periods"][0]["sources"][0]
    assert source["content_type"] == "application/pdf"
    assert source["warnings"] == []


def test_source_role_appears_in_validation_report(db_session):
    doc = ReportDocument(
        company_id=1,
        report_period="2021Q1",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="issuer_ir_manifest",
        source_url="https://www.lukoil.com/report.pdf",
        file_name="report.pdf",
        file_hash="hash",
        status="parsed",
    )
    db_session.add(doc)
    db_session.flush()
    fact = StatementFact(
        company_id=1,
        report_document_id=doc.id,
        period="2021Q1",
        reporting_standard="IFRS",
        statement_type="income_statement",
        metric_code="revenue",
        value=100,
        currency="RUB",
        unit_multiplier=1_000_000,
        period_type="quarter",
        source_location={"source_type": doc.source_type, "source_url": doc.source_url, "page": 1},
        quality_flag="exact",
        confidence_score=0.95,
    )
    db_session.add(fact)
    job = AnalysisJob(company_query="LKOH", period_from="2021Q1", period_to="2021Q1", data_mode="real")
    db_session.add(job)
    db_session.flush()
    result = AnalysisResult(
        job_id=job.id,
        company_id=1,
        period_from="2021Q1",
        period_to="2021Q1",
        data_snapshot_json={},
        result_json={"data_quality": {"real_data_used": True, "fixture_data_used": False}, "financial_analysis": {"metrics": []}},
        llm_payload_json={},
        disclaimer="test",
    )
    db_session.add(result)
    db_session.commit()
    report = build_validation_report(db_session, db_session.get(Company, 1), "2021Q1", "2021Q1", job, result)
    assert report["documents"][0]["source_role"] == "financial_statements"
    assert report["source_role_summary"]["financial_statements"] == 1


def test_validation_cannot_pass_with_only_press_release_sources(db_session):
    doc = ReportDocument(
        company_id=1,
        report_period="2021Q1",
        reporting_standard="IFRS",
        document_type="press_release",
        source_role="press_release",
        source_type="issuer_ir_manifest",
        source_url="https://www.lukoil.com/release.pdf",
        file_name="release.pdf",
        file_hash="hash",
        status="parsed",
    )
    db_session.add(doc)
    db_session.flush()
    for index, metric_code in enumerate(KEY_FACTS, start=1):
        db_session.add(
            StatementFact(
                company_id=1,
                report_document_id=doc.id,
                period="2021Q1",
                reporting_standard="IFRS",
                statement_type="income_statement",
                metric_code=metric_code,
                value=float(index),
                currency="RUB",
                unit_multiplier=1_000_000,
                period_type="quarter",
                source_location={"source_type": doc.source_type, "source_url": doc.source_url, "page": 1},
                quality_flag="exact",
                confidence_score=0.95,
            )
        )
    job = AnalysisJob(company_query="LKOH", period_from="2021Q1", period_to="2021Q1", data_mode="real")
    db_session.add(job)
    db_session.flush()
    result = AnalysisResult(
        job_id=job.id,
        company_id=1,
        period_from="2021Q1",
        period_to="2021Q1",
        data_snapshot_json={},
        result_json={"data_quality": {"real_data_used": True, "fixture_data_used": False}, "financial_analysis": {"metrics": []}},
        llm_payload_json={},
        disclaimer="test",
    )
    db_session.add(result)
    db_session.commit()
    report = build_validation_report(db_session, db_session.get(Company, 1), "2021Q1", "2021Q1", job, result)
    assert report["status"] == "FAIL"
    assert report["source_role_summary"]["press_release"] == 1
