import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from app.db.models import MetricValue, ReportDocument, StatementFact
from app.services.company_source_discovery import CompanySourceCandidate, CompanySourceDiscoveryReport
from app.services.coverage.universal_coverage_scanner import CoverageScanRequest, UniversalCoverageScanner
from app.services.reports.manual_report_ingestion import (
    ManualReportIngestionRequest,
    ManualReportIngestionService,
)
from app.tools import ingest_manual_report as cli


def runtime_root() -> Path:
    root = Path("tests/runtime_manual_report_ingestion") / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root


def fake_ifrs_pdf(path: Path, ticker: str = "LKOH", year: int = 2021) -> Path:
    path.write_bytes(
        (
            "%PDF-1.4\n"
            f"{ticker}\n"
            "IFRS\n"
            "consolidated financial statements\n"
            "statement of financial position\n"
            "statement of profit or loss\n"
            f"year ended 31 December {year}\n"
        ).encode()
    )
    return path


def invalid_pdf(path: Path, ticker: str = "LKOH") -> Path:
    path.write_bytes(f"%PDF-1.4\n{ticker}\npress release\n".encode())
    return path


def annual_like_pdf(path: Path, company_name: str = "Magnit") -> Path:
    path.write_bytes(
        (
            "%PDF-1.4\n"
            f"{company_name}\n"
            "annual report\n"
            "revenue 1200\n"
            "operating profit 240\n"
            "net income 180\n"
        ).encode()
    )
    return path


def test_manual_pdf_is_copied_sha256_and_report_document_created(db_session):
    root = runtime_root()
    source = fake_ifrs_pdf(root / "L KOH FY report.pdf")
    service = ManualReportIngestionService(db_session, root=root)

    report = service.ingest(
        ManualReportIngestionRequest(
            company_ticker="LKOH",
            reporting_standard="IFRS",
            period="2021Q4",
            local_file_path=str(source),
            manual_upload_reason="manual_test",
            run_table_extraction=False,
        )
    )

    document = db_session.get(ReportDocument, report.report_document_id)
    assert report.sha256
    assert report.stored_document_path
    assert Path(report.stored_document_path).exists()
    assert Path(report.stored_document_path).name.endswith("_L_KOH_FY_report.pdf")
    assert document.source_type == "manual_upload"
    assert document.manual_upload_reason == "manual_test"
    assert document.source_trust_bucket == "manual_upload_validated"
    assert document.official_source_verified is False
    assert document.source_package_ready_contribution is False
    assert report.status == "INGESTED"


def test_manual_upload_overrides_default_period_from_document_text(db_session):
    root = runtime_root()
    source = fake_ifrs_pdf(root / "report.pdf", ticker="MGNT", year=2025)
    service = ManualReportIngestionService(db_session, root=root)

    report = service.ingest(
        ManualReportIngestionRequest(
            company_ticker="MGNT",
            reporting_standard="IFRS",
            period="2026Q4",
            local_file_path=str(source),
            run_table_extraction=False,
        )
    )

    document = db_session.get(ReportDocument, report.report_document_id)
    assert report.period == "2025Q4"
    assert report.original_period == "2026Q4"
    assert report.effective_report_period == "2025Q4"
    assert report.period_source == "document_text"
    assert report.period_confidence and report.period_confidence >= 0.8
    assert document.report_period == "2025Q4"
    assert report.degraded_primary_statements_found == 0
    assert report.top_structural_blockers == []


def test_file_safety_rejects_bad_inputs(db_session):
    root = runtime_root()
    service = ManualReportIngestionService(db_session, root=root)
    missing = root / "missing.pdf"
    directory = root / "dir.pdf"
    directory.mkdir()
    (root / "bad.txt").write_text("x", encoding="utf-8")
    (root / "old.xls").write_text("x", encoding="utf-8")
    large = root / "large.pdf"
    large.write_bytes(b"x" * 2)
    original_limit = service.settings.max_report_download_mb
    service.settings.max_report_download_mb = 0

    cases = [
        (missing, "manual_upload_file_not_found"),
        (directory, "manual_upload_path_not_regular_file"),
        (root / "bad.txt", "manual_upload_extension_not_allowed"),
        (root / "old.xls", "xls_not_supported_in_v1"),
        (large, "manual_upload_file_too_large"),
    ]
    try:
        for path, blocker in cases:
            report = service.ingest(
                ManualReportIngestionRequest(
                    company_ticker="LKOH",
                    reporting_standard="IFRS",
                    period="2021Q4",
                    local_file_path=str(path),
                )
            )
            assert blocker in report.blockers
            assert report.report_document_id is None
    finally:
        service.settings.max_report_download_mb = original_limit


def test_zip_safety_rejects_unsafe_archives(db_session):
    root = runtime_root()
    service = ManualReportIngestionService(db_session, root=root)
    cases = []
    traversal = root / "traversal.zip"
    with ZipFile(traversal, "w") as archive:
        archive.writestr("../evil.pdf", "x")
    cases.append((traversal, "manual_upload_zip_path_traversal"))
    unsupported = root / "unsupported.zip"
    with ZipFile(unsupported, "w") as archive:
        archive.writestr("evil.exe", "x")
    cases.append((unsupported, "manual_upload_zip_unsupported_internal_file"))
    xls = root / "xls.zip"
    with ZipFile(xls, "w") as archive:
        archive.writestr("old.xls", "x")
    cases.append((xls, "xls_not_supported_in_v1"))
    too_large = root / "too_large.zip"
    original_limit = service.settings.max_report_download_mb
    service.settings.max_report_download_mb = 0
    with ZipFile(too_large, "w") as archive:
        archive.writestr("report.pdf", "xx")
    cases.append((too_large, "manual_upload_zip_decompressed_too_large"))

    try:
        for path, blocker in cases:
            report = service.ingest(
                ManualReportIngestionRequest(
                    company_ticker="LKOH",
                    reporting_standard="IFRS",
                    period="2021Q4",
                    local_file_path=str(path),
                )
            )
            assert blocker in report.blockers
            assert report.report_document_id is None
    finally:
        service.settings.max_report_download_mb = original_limit


def test_duplicate_upload_returns_existing_document(db_session):
    root = runtime_root()
    source = fake_ifrs_pdf(root / "report.pdf")
    service = ManualReportIngestionService(db_session, root=root)
    request = ManualReportIngestionRequest(
        company_ticker="LKOH",
        reporting_standard="IFRS",
        period="2021Q4",
        local_file_path=str(source),
        run_table_extraction=False,
    )

    first = service.ingest(request)
    second = service.ingest(request)

    assert first.duplicate_detected is False
    assert second.duplicate_detected is True
    assert second.report_document_id == first.report_document_id
    assert db_session.query(ReportDocument).filter(ReportDocument.source_type == "manual_upload").count() == 1


def test_validation_failure_keeps_audit_document_and_blocks_facts(db_session):
    root = runtime_root()
    source = invalid_pdf(root / "bad.pdf")
    before_facts = db_session.query(StatementFact).count()

    report = ManualReportIngestionService(db_session, root=root).ingest(
        ManualReportIngestionRequest(
            company_ticker="LKOH",
            reporting_standard="IFRS",
            period="2021Q4",
            local_file_path=str(source),
            run_dataframe_fact_parser=True,
        )
    )

    document = db_session.get(ReportDocument, report.report_document_id)
    assert report.ingestion_status in {"EVIDENCE_ONLY", "INVALID_UPLOAD"}
    assert report.document_validation_status in {"fail", "partial"}
    assert any(blocker.startswith("document_validation_failed") for blocker in report.blockers)
    assert document.status in {"rejected", "evidence_only"}
    assert document.source_trust_bucket == "manual_upload_unverified"
    assert db_session.query(StatementFact).count() == before_facts


def test_unknown_ticker_can_bootstrap_identity_for_manual_upload(monkeypatch, db_session):
    import app.services.reports.manual_report_ingestion as module

    root = runtime_root()
    source = fake_ifrs_pdf(root / "mgnt_report.pdf", ticker="MGNT")

    class FakeDiscoveryService:
        def __init__(self, db, root=None):
            self.db = db
            self.root = root

        def discover(self, query, ticker=None, live=False):
            candidate = CompanySourceCandidate(
                candidate_type="identity",
                source_url="https://iss.moex.com/iss/securities.json",
                source_domain="iss.moex.com",
                source_provider="moex_iss",
                matched_name="ПАО Магнит",
                ticker="MGNT",
                confidence_score=0.9,
                verification_scope="identity_only",
                provenance={"ticker": "MGNT", "board": "TQBR", "name": "ПАО Магнит", "short_name": "Магнит"},
            )
            return CompanySourceDiscoveryReport(
                query=query,
                resolved_company=None,
                recommended_candidates=[candidate],
                auto_select_allowed=True,
            )

        def save_report(self, report):
            path = root / "data" / "validation" / "company_source_discovery" / "mgnt_source_discovery_report.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            return path

    monkeypatch.setattr(module, "CompanySourceDiscoveryService", FakeDiscoveryService)
    report = ManualReportIngestionService(db_session, root=root).ingest(
        ManualReportIngestionRequest(
            company_ticker="MGNT",
            reporting_standard="IFRS",
            period="2021Q4",
            local_file_path=str(source),
            run_table_extraction=False,
        )
    )

    assert report.identity_status == "bootstrapped_from_moex_iss"
    assert report.identity_report_path
    assert report.ingestion_status == "VALIDATED_FINANCIAL_STATEMENT"
    assert db_session.query(ReportDocument).filter(ReportDocument.source_type == "manual_upload").count() == 1


def test_unknown_company_falls_back_to_pending_identity_and_evidence_only(monkeypatch, db_session):
    import app.services.reports.manual_report_ingestion as module

    root = runtime_root()
    source = annual_like_pdf(root / "annual.pdf")

    class FakeDiscoveryService:
        def __init__(self, db, root=None):
            self.db = db
            self.root = root

        def discover(self, query, ticker=None, live=False):
            return CompanySourceDiscoveryReport(
                query=query,
                resolved_company=None,
                recommended_candidates=[],
                auto_select_allowed=False,
                warnings=["no_exact_identity_match"],
            )

        def save_report(self, report):
            path = root / "data" / "validation" / "company_source_discovery" / "annual_source_discovery_report.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            return path

    class FakeValidator:
        def validate(self, document, expected_file_type=None, max_text_pages=None):
            return SimpleNamespace(
                validation_status="fail",
                detected_document_role="annual_report",
                warnings=["company_marker_weak"],
                to_dict=lambda: {
                    "validation_status": "fail",
                    "detected_document_role": "annual_report",
                    "warnings": ["company_marker_weak"],
                },
            )

    class FakeExtractor:
        def __init__(self, root=None):
            self.root = root

        def extract(self, document):
            artifact = root / "data" / "parsed" / document.company.ticker / "2021Q4" / f"{document.id}_statement_tables.json"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "document_id": document.id,
                "statement_tables_count": 0,
                "required_statement_tables_found": False,
                "statement_coverage": {},
                "artifact_path": str(artifact),
                "statement_tables": [],
                "warnings": ["text_table_fallback_used"],
            }
            artifact.write_text(json.dumps(payload), encoding="utf-8")
            return payload

    class FakeParser:
        def __init__(self, *args, **kwargs):
            pass

        def parse(self, *args, **kwargs):
            return SimpleNamespace(
                status="PARTIAL",
                canonical_facts_created=0,
                to_dict=lambda: {
                    "status": "PARTIAL",
                    "canonical_facts_created": 0,
                    "structured_facts": [],
                    "rejected_rows": [{"raw_label": "Revenue", "rejection_reason": "policy_blocked"}],
                    "unmapped_numeric_evidence": [
                        {"raw_label": "Revenue", "not_confirmed_fact": True, "not_for_ratio_calculation": True}
                    ],
                    "unmapped_table_evidence": [],
                    "llm_ready_evidence_pack": {"normalized_facts": [], "unresolved_numeric_evidence": [{}]},
                },
            )

    monkeypatch.setattr(module, "CompanySourceDiscoveryService", FakeDiscoveryService)
    monkeypatch.setattr(module, "DocumentValidator", lambda: FakeValidator())
    monkeypatch.setattr(module, "StatementTableExtractor", FakeExtractor)
    monkeypatch.setattr(module, "DataFrameStatementParser", FakeParser)

    report = ManualReportIngestionService(db_session, root=root).ingest(
        ManualReportIngestionRequest(
            company_ticker="Some New Issuer",
            reporting_standard="IFRS",
            period="2021Q4",
            local_file_path=str(source),
            run_table_extraction=False,
            run_dataframe_fact_parser=False,
        )
    )

    document = db_session.get(ReportDocument, report.report_document_id)
    assert report.identity_status == "pending_manual_resolution"
    assert report.ingestion_status == "EVIDENCE_ONLY"
    assert report.document_classification == "evidence_only_report"
    assert report.evidence_pack_available is True
    assert report.rejected_rows_count == 1
    assert report.unmapped_numeric_evidence_count == 1
    assert document.status == "evidence_only"


def test_valid_upload_runs_table_extraction_and_saves_artifact(monkeypatch, db_session):
    import app.services.reports.manual_report_ingestion as module

    root = runtime_root()
    source = fake_ifrs_pdf(root / "report.pdf")

    class FakeExtractor:
        def __init__(self, root=None):
            self.root = root

        def extract(self, document):
            artifact = root / "data" / "parsed" / "LKOH" / "2021Q4" / f"{document.id}_statement_tables.json"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "document_id": document.id,
                "statement_tables_count": 2,
                "required_statement_tables_found": True,
                "statement_coverage": {
                    "balance_sheet": {"found": True, "count": 1, "periods": ["2021Q4"]},
                    "income_statement": {"found": True, "count": 1, "periods": ["2021Q4"]},
                    "cash_flow": {"found": False, "count": 0, "periods": []},
                },
                "facts_extracted": 0,
                "fact_parser_status": "not_invoked",
                "artifact_path": str(artifact),
                "warnings": [],
            }
            artifact.write_text(json.dumps(payload), encoding="utf-8")
            return payload

    monkeypatch.setattr(module, "StatementTableExtractor", FakeExtractor)
    report = ManualReportIngestionService(db_session, root=root).ingest(
        ManualReportIngestionRequest(
            company_ticker="LKOH",
            reporting_standard="IFRS",
            period="2021Q4",
            local_file_path=str(source),
        )
    )

    assert report.statement_tables_extracted == 2
    assert report.statement_coverage["balance_sheet"]["found"] is True
    assert report.dataframe_artifacts
    assert Path(report.dataframe_artifacts[0]).exists()
    assert report.fact_parse_status == "not_invoked"


def test_dataframe_parser_runs_only_with_explicit_flag_and_never_persists(monkeypatch, db_session):
    import app.services.reports.manual_report_ingestion as module

    root = runtime_root()
    source = fake_ifrs_pdf(root / "report.pdf")
    calls = {"parser": 0, "args": None}

    class FakeParser:
        def __init__(self, *args, **kwargs):
            pass

        def parse(self, *args, **kwargs):
            calls["parser"] += 1
            calls["args"] = args
            return SimpleNamespace(
                status="PARTIAL",
                canonical_facts_created=3,
                to_dict=lambda: {"status": "PARTIAL", "canonical_facts_created": 3},
            )

    monkeypatch.setattr(module, "DataFrameStatementParser", FakeParser)
    service = ManualReportIngestionService(db_session, root=root)
    first = service.ingest(
        ManualReportIngestionRequest(
            company_ticker="LKOH",
            reporting_standard="IFRS",
            period="2021Q4",
            local_file_path=str(source),
            run_table_extraction=False,
        )
    )
    second = service.ingest(
        ManualReportIngestionRequest(
            company_ticker="LKOH",
            reporting_standard="IFRS",
            period="2021Q4",
            local_file_path=str(source),
            run_table_extraction=False,
            run_dataframe_fact_parser=True,
        )
    )

    assert first.fact_parse_status == "not_invoked"
    assert second.fact_parse_status == "PARTIAL"
    assert second.canonical_fact_candidates == 3
    assert second.db_persisted is False
    assert calls["parser"] == 1
    assert calls["args"][1:3] == ("2021Q4", "2021Q4")


def test_manual_upload_plain_year_uses_full_year_range_for_dataframe_parser(monkeypatch, db_session):
    import app.services.reports.manual_report_ingestion as module

    root = runtime_root()
    source = fake_ifrs_pdf(root / "report.pdf", year=2025)
    calls = {"args": None}

    class FakeParser:
        def __init__(self, *args, **kwargs):
            pass

        def parse(self, *args, **kwargs):
            calls["args"] = args
            return SimpleNamespace(
                status="PARTIAL",
                canonical_facts_created=0,
                to_dict=lambda: {"status": "PARTIAL", "canonical_facts_created": 0},
            )

    monkeypatch.setattr(module, "DataFrameStatementParser", FakeParser)
    report = ManualReportIngestionService(db_session, root=root).ingest(
        ManualReportIngestionRequest(
            company_ticker="LKOH",
            reporting_standard="IFRS",
            period="2025",
            local_file_path=str(source),
            run_table_extraction=False,
            run_dataframe_fact_parser=True,
        )
    )

    assert report.fact_parse_status == "PARTIAL"
    assert calls["args"][1:3] == ("2025Q4", "2025Q4")


def test_cli_has_no_persist_flag_and_writes_report(monkeypatch, db_session):
    class SessionContext:
        def __enter__(self):
            return db_session

        def __exit__(self, *_args):
            return None

    root = runtime_root()
    source = fake_ifrs_pdf(root / "report.pdf")
    original_init = cli.ManualReportIngestionService.__init__

    def patched_init(self, db, root=None):
        original_init(self, db, root=root)

    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "SessionLocal", lambda: SessionContext())
    monkeypatch.setattr(cli.ManualReportIngestionService, "__init__", patched_init)

    with pytest.raises(SystemExit):
        cli.main(["LKOH", "2021Q4", "--file", str(source), "--persist-facts"])
    report, path = cli.ingest_manual_report("LKOH", "2021Q4", "IFRS", str(source))
    saved = json.loads(Path(path).read_text(encoding="utf-8"))
    assert saved["db_persisted"] is False
    assert saved["source_package_ready_contribution"] is False


def test_manual_upload_does_not_mutate_manifests_or_metrics(db_session):
    root = runtime_root()
    source = fake_ifrs_pdf(root / "report.pdf")
    manifests = {path: path.read_bytes() for path in Path("data/manifests").glob("**/*") if path.is_file()}
    before_metrics = db_session.query(MetricValue).count()

    ManualReportIngestionService(db_session, root=root).ingest(
        ManualReportIngestionRequest(
            company_ticker="LKOH",
            reporting_standard="IFRS",
            period="2021Q4",
            local_file_path=str(source),
            run_table_extraction=False,
        )
    )

    assert db_session.query(MetricValue).count() == before_metrics
    for path, content in manifests.items():
        assert path.read_bytes() == content


def test_coverage_scanner_reports_manual_upload_separately(db_session):
    root = runtime_root()
    source = fake_ifrs_pdf(root / "report.pdf", ticker="TATN")
    ManualReportIngestionService(db_session, root=root).ingest(
        ManualReportIngestionRequest(
            company_ticker="TATN",
            reporting_standard="IFRS",
            period="2021Q4",
            local_file_path=str(source),
            manual_upload_reason="missing_fy_report",
            run_table_extraction=False,
        )
    )

    report = UniversalCoverageScanner(db_session, root=root).scan(
        CoverageScanRequest(
            tickers=["TATN"],
            from_registry=False,
            period_from="2021Q4",
            period_to="2021Q4",
            reporting_standard="IFRS",
        )
    )
    item = report.company_items[0]
    assert item["documents_status"] == "CACHED_VALIDATED_MANUAL"
    assert "manual_upload_available" in item["blockers"]
    assert "manual_document_analysis" in item["supported_outputs"]
    assert item["manual_upload"]["reasons"] == ["missing_fy_report"]


def teardown_module():
    shutil.rmtree("tests/runtime_manual_report_ingestion", ignore_errors=True)
