import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pandas as pd

from app.api.routes import reports as reports_route
from app.db.models import Company, ReportDocument
from app.services.market.market_audit import period_date_range
from app.services.ui.pipeline_control_center import (
    PIPELINE_JOB_STORE,
    FullPipelineRunRequest,
    PipelineControlCenter,
    PipelineRunRequest,
    PipelineStageResult,
    SourcePipelineRunRequest,
)


def runtime_root() -> Path:
    root = Path("tests") / "runtime_pipeline_control_center" / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def test_control_center_missing_market_cache_is_blocked_without_live_fetch(db_session, monkeypatch):
    root = runtime_root()

    def fail_live(*args, **kwargs):  # pragma: no cover - should never be called
        raise AssertionError("live MOEX fetch must not be called")

    def fake_save_market_report(report):
        path = root / "data" / "validation" / report["ticker"].upper() / (
            f"{report['period_from']}_{report['period_to']}_market_technical_report.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        return path

    monkeypatch.setattr("app.services.market.moex_client.MoexClient.get_security_candles", fail_live)
    monkeypatch.setattr("app.services.ui.pipeline_control_center.save_market_technical_report", fake_save_market_report)
    result = PipelineControlCenter(db_session, root=root).run_safe_report_pipeline(
        PipelineRunRequest(ticker="LKOH", period_from="2021Q1", period_to="2021Q4")
    )
    market_stage = next(stage for stage in result.stages if stage.stage == "market_technical")
    assert result.overall_status == "PARTIAL"
    assert market_stage.status == "BLOCKED"
    assert market_stage.reason == "cache_missing"
    assert market_stage.recommended_action == "run_market_live_explicitly"
    assert market_stage.action_taken == "read from replay-cache"
    assert market_stage.report_path
    assert not Path(market_stage.report_path).is_absolute()
    shutil.rmtree(root, ignore_errors=True)


def test_control_center_market_replay_cache_can_pass_without_live_fetch(db_session, monkeypatch):
    root = runtime_root()
    from_date, to_date = period_date_range("2021Q1", "2021Q4")
    cache_path = root / "data" / "market_cache" / "LKOH" / "TQBR" / f"{from_date}_{to_date}_candles.csv"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    dates = pd.date_range("2021-01-01", periods=220, freq="B")
    pd.DataFrame(
        {
            "date": dates.date,
            "open": range(220, 440),
            "high": range(221, 441),
            "low": range(219, 439),
            "close": range(220, 440),
            "volume": [1000] * 220,
            "value": [100000] * 220,
        }
    ).to_csv(cache_path, index=False)

    def fail_live(*args, **kwargs):  # pragma: no cover - should never be called
        raise AssertionError("live MOEX fetch must not be called")

    def fake_save_market_report(report):
        path = root / "data" / "validation" / report["ticker"].upper() / (
            f"{report['period_from']}_{report['period_to']}_market_technical_report.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        return path

    monkeypatch.setattr("app.services.market.moex_client.MoexClient.get_security_candles", fail_live)
    monkeypatch.setattr("app.services.ui.pipeline_control_center.save_market_technical_report", fake_save_market_report)
    result = PipelineControlCenter(db_session, root=root).run_safe_report_pipeline(
        PipelineRunRequest(ticker="LKOH", period_from="2021Q1", period_to="2021Q4")
    )
    market_stage = next(stage for stage in result.stages if stage.stage == "market_technical")
    assert market_stage.status in {"PASS", "PARTIAL"}
    assert market_stage.action_taken == "read from replay-cache"
    shutil.rmtree(root, ignore_errors=True)


def test_pipeline_status_is_read_only_and_uses_relative_paths(db_session):
    root = runtime_root()
    report = root / "data" / "validation" / "LKOH" / "2021Q1_2021Q4_financial_ratios.json"
    machine_report = root / "data" / "validation" / "LKOH" / "2021Q4_machine_report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text('{"summary":{"calculated_count":1}}', encoding="utf-8")
    machine_report.write_text('{"machine_report_schema_version":"1.0"}', encoding="utf-8")
    status = PipelineControlCenter(db_session, root=root).status("LKOH", "2021Q1", "2021Q4")
    ratios = next(item for item in status["reports"] if item["name"] == "financial_ratios")
    machine = next(item for item in status["reports"] if item["name"] == "machine_report")
    assert ratios["exists"] is True
    assert ratios["path"] == "data/validation/LKOH/2021Q1_2021Q4_financial_ratios.json"
    assert machine["exists"] is True
    assert machine["path"] == "data/validation/LKOH/2021Q4_machine_report.json"
    assert status["safety"]["artifacts_created"] is False
    assert status["safety"]["facts_persisted"] is False
    shutil.rmtree(root, ignore_errors=True)


def test_draft_candidate_analysis_uses_dataframe_candidates_without_persisting(db_session, monkeypatch):
    root = runtime_root()
    parse_path = root / "data" / "validation" / "SBER" / "2025Q1_2025Q4_dataframe_statement_fact_parse.json"
    parse_path.parent.mkdir(parents=True, exist_ok=True)
    parse_path.write_text(
        """
        {
          "company_ticker": "SBER",
          "period_from": "2025Q1",
          "period_to": "2025Q4",
          "reporting_standard": "IFRS",
          "structured_facts": [
            {"metric_code": "total_assets"},
            {"metric_code": "total_equity"}
          ],
          "derived_safe_facts": [],
          "rejected_rows": [
            {"rejection_reason": "industrial_concept_not_banking_metric"}
          ],
          "unmapped_numeric_evidence": [
            {"raw_label": "Unknown line", "not_confirmed_fact": true, "not_for_ratio_calculation": true}
          ],
          "unmapped_table_evidence": [
            {"table_title": "Note 12", "not_confirmed_fact": true, "not_for_ratio_calculation": true}
          ],
          "llm_ready_evidence_pack": {
            "normalized_facts": [{"metric_code": "total_assets"}],
            "derived_safe_facts": [],
            "rejected_rows": [{"rejection_reason": "industrial_concept_not_banking_metric"}],
            "unresolved_numeric_evidence": [{"raw_label": "Unknown line"}],
            "unresolved_table_evidence": [{"table_title": "Note 12"}]
          },
          "facts": [
            {
              "company_ticker": "SBER",
              "period": "2025Q4",
              "reporting_standard": "IFRS",
              "metric_code": "total_assets",
              "value": 1000,
              "period_type": "balance_sheet_snapshot",
              "quality_flag": "high_confidence_text_fallback",
              "extraction_method": "text_table_fallback_semantic_gate",
              "source_location": {"fact_source_kind": "text_table_fallback_semantic_gate"}
            },
            {
              "company_ticker": "SBER",
              "period": "2025Q4",
              "reporting_standard": "IFRS",
              "metric_code": "total_equity",
              "value": 100,
              "period_type": "balance_sheet_snapshot",
              "quality_flag": "high_confidence_text_fallback",
              "extraction_method": "text_table_fallback_semantic_gate",
              "source_location": {"fact_source_kind": "text_table_fallback_semantic_gate"}
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    monkeypatch.setattr(
        PipelineControlCenter,
        "_run_peer_analysis",
        lambda self, request: PipelineStageResult(
            stage="peer_analysis",
            status="BLOCKED",
            action_taken="generated from existing ratios",
            reason="peer_comparison_not_ready",
        ),
    )
    monkeypatch.setattr(
        PipelineControlCenter,
        "_run_llm_payload",
        lambda self, request: PipelineStageResult(stage="llm_payload", status="PASS", action_taken="built, LLM not invoked"),
    )
    monkeypatch.setattr(
        PipelineControlCenter,
        "_run_demo_report",
        lambda self, request: PipelineStageResult(stage="demo_report", status="PASS", action_taken="generated"),
    )

    result = PipelineControlCenter(db_session, root=root).run_draft_candidate_analysis(
        PipelineRunRequest(ticker="SBER", period_from="2025Q1", period_to="2025Q4")
    )
    ratios_stage = next(stage for stage in result.stages if stage.stage == "financial_ratios")
    ratios_path = root / "data" / "validation" / "SBER" / "2025Q1_2025Q4_financial_ratios.json"
    assert result.overall_status == "PARTIAL"
    assert result.safety["analysis_trust_level"] == "manual_upload_candidate_based"
    assert result.safety["facts_persisted"] is False
    assert ratios_stage.action_taken == "generated from DataFrame candidates"
    assert ratios_stage.summary["facts_source"] == "dataframe_parse_report"
    assert ratios_stage.summary["text_fallback_candidates_allowed"] is True
    assert ratios_path.exists()
    assert "metric_uses_text_fallback_fact_candidates" in ratios_path.read_text(encoding="utf-8")
    shutil.rmtree(root, ignore_errors=True)


def test_parse_stage_exposes_v2_evidence_counts(db_session, monkeypatch):
    root = runtime_root()
    company = db_session.query(Company).filter_by(ticker="LKOH", board="TQBR").one()
    fake_result = SimpleNamespace(
        to_dict=lambda: {
            "company_ticker": "LKOH",
            "period_from": "2021Q1",
            "period_to": "2021Q4",
            "reporting_standard": "IFRS",
            "documents_processed": 1,
            "tables_processed": 2,
            "canonical_facts_created": 3,
            "facts": [{"metric_code": "revenue"}],
            "structured_facts": [{"metric_code": "revenue"}, {"metric_code": "net_income"}],
            "derived_safe_facts": [{"derived_metric_code": "customer_accounts"}],
            "rejected_rows": [{"rejection_reason": "policy_blocked"}],
            "unmapped_numeric_evidence": [
                {
                    "raw_label": "Unknown line",
                    "source_engine": "ocr_table_structure_engine",
                    "source_engines_involved": ["ocr_table_structure_engine"],
                }
            ],
            "unmapped_table_evidence": [{"table_title": "Note 12"}],
            "llm_ready_evidence_pack": {"normalized_facts": [{"metric_code": "revenue"}]},
            "analysis_readiness_summary": {"coverage_grade": "partial"},
            "warnings": [],
        }
    )
    monkeypatch.setattr(
        "app.services.ui.pipeline_control_center.DataFrameStatementParser.parse",
        lambda *args, **kwargs: fake_result,
    )

    stage = PipelineControlCenter(db_session, root=root)._parse_dataframe_facts(
        company,
        SourcePipelineRunRequest(ticker="LKOH", period_from="2021Q1", period_to="2021Q4"),
    )

    assert stage.summary["structured_facts_count"] == 2
    assert stage.summary["derived_safe_facts_count"] == 1
    assert stage.summary["rejected_rows_count"] == 1
    assert stage.summary["unmapped_numeric_evidence_count"] == 1
    assert stage.summary["unmapped_table_evidence_count"] == 1
    assert stage.summary["llm_ready_evidence_pack_available"] is True
    assert stage.summary["engine_contribution_summary"]["merged_fact_count"] == 0
    assert stage.summary["engine_contribution_summary"]["ocr_only_fact_count"] == 0
    assert "ocr_table_structure_engine" in stage.summary["engine_contribution_summary"]["engines_with_evidence_only_contribution"]
    shutil.rmtree(root, ignore_errors=True)


def test_app_and_pipeline_routes_expose_safe_labels(client, monkeypatch):
    class FakeControlCenter:
        def __init__(self, db):
            self.db = db

        def run_safe_report_pipeline(self, request):
            return SimpleNamespace(
                to_dict=lambda: {
                    "overall_status": "PARTIAL",
                    "stages": [
                        {
                            "stage": "market_technical",
                            "status": "BLOCKED",
                            "action_taken": "read from replay-cache",
                            "reason": "cache_missing",
                            "recommended_action": "run_market_live_explicitly",
                            "report_path": "data/validation/LKOH/report.json",
                        }
                    ],
                }
            )

        def status(self, **kwargs):
            return {"reports": [], "safety": {"read_only_status_check": True}}

    monkeypatch.setattr("app.api.routes.pipeline.PipelineControlCenter", FakeControlCenter)
    html = client.get("/app")
    assert html.status_code == 200
    assert "Загрузите PDF и запустите анализ" in html.text
    assert "Добавить PDF" in html.text
    assert "Что сказать на защите" in html.text
    assert "Итог чернового анализа" in html.text
    response = client.post(
        "/pipeline/demo-run",
        json={"ticker": "LKOH", "period_from": "2021Q1", "period_to": "2021Q4"},
    )
    payload = response.json()
    assert payload["overall_status"] == "PARTIAL"
    assert payload["stages"][0]["recommended_action"] == "run_market_live_explicitly"


def test_source_pipeline_can_bootstrap_exact_moex_identity_and_continue(db_session, monkeypatch):
    root = runtime_root()

    class FakeSourceReport:
        query = "IRAO"
        warnings = []
        auto_select_allowed = False
        recommended_candidates = [
            SimpleNamespace(
                source_provider="moex_iss",
                verification_scope="identity_only",
                ticker="FIXIRAO",
                matched_name="Fixing",
                provenance={"ticker": "FIXIRAO", "board": "INPF"},
            ),
            SimpleNamespace(
                source_provider="moex_iss",
                verification_scope="identity_only",
                ticker="IRAO",
                matched_name="Inter RAO",
                provenance={
                    "ticker": "IRAO",
                    "board": "TQBR",
                    "isin": "RU000A0JPNM1",
                    "name": "Inter RAO",
                    "short_name": "IRAO",
                },
            ),
        ]

        def to_dict(self):
            return {"query": self.query, "recommended_candidates": [], "warnings": []}

    class FakeSourceDiscoveryService:
        def __init__(self, db, root=None):
            self.root = root

        def discover(self, *args, **kwargs):
            return FakeSourceReport()

        def save_report(self, report):
            path = root / "data" / "validation" / "company_source_discovery" / "irao_source_discovery_report.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
            return path

    monkeypatch.setattr("app.services.ui.pipeline_control_center.CompanySourceDiscoveryService", FakeSourceDiscoveryService)
    result = PipelineControlCenter(db_session, root=root).run_source_discovery_pipeline(
        SourcePipelineRunRequest(ticker="IRAO", period_from="2021Q1", period_to="2021Q4")
    )
    assert result.overall_status in {"PARTIAL", "BLOCKED"}
    stage = result.stages[0]
    assert stage.stage == "identity_resolution"
    assert stage.status == "PASS"
    assert stage.action_taken == "bootstrapped identity from MOEX ISS exact ticker match"
    assert stage.summary["trust_scope"] == "identity_only"
    assert stage.report_path == "data/validation/company_source_discovery/irao_source_discovery_report.json"
    assert result.safety["facts_persisted"] is False
    assert result.safety["manifest_mutated"] is False
    shutil.rmtree(root, ignore_errors=True)


def test_source_pipeline_api_accepts_auto_bootstrap_flag(client, monkeypatch):
    class FakeControlCenter:
        def __init__(self, db):
            self.db = db

        def run_source_discovery_pipeline(self, request):
            assert request.auto_bootstrap_identity is True
            return SimpleNamespace(
                to_dict=lambda: {
                    "overall_status": "PARTIAL",
                    "stages": [
                        {
                            "stage": "identity_resolution",
                            "status": "PASS",
                            "action_taken": "bootstrapped identity from MOEX ISS exact ticker match",
                            "report_path": "data/validation/company_source_discovery/irao_source_discovery_report.json",
                        }
                    ],
                    "safety": {"facts_persisted": False, "manifest_mutated": False},
                }
            )

    monkeypatch.setattr("app.api.routes.pipeline.PipelineControlCenter", FakeControlCenter)
    response = client.post(
        "/pipeline/source-run",
        json={"ticker": "IRAO", "period_from": "2021Q1", "period_to": "2021Q4"},
    )
    payload = response.json()
    assert response.status_code == 200
    assert payload["overall_status"] == "PARTIAL"
    assert payload["stages"][0]["status"] == "PASS"
    assert payload["safety"]["facts_persisted"] is False


def test_pipeline_api_accepts_plain_year_as_full_year_range(client, monkeypatch):
    captured = {}

    class FakeControlCenter:
        def __init__(self, db):
            self.db = db

        def run_safe_report_pipeline(self, request):
            captured["period_from"] = request.period_from
            captured["period_to"] = request.period_to
            return SimpleNamespace(to_dict=lambda: {"overall_status": "PARTIAL", "stages": []})

    monkeypatch.setattr("app.api.routes.pipeline.PipelineControlCenter", FakeControlCenter)
    response = client.post(
        "/pipeline/demo-run",
        json={"ticker": "SBER", "period_from": "2025", "period_to": "2025"},
    )
    assert response.status_code == 200
    assert captured == {"period_from": "2025Q1", "period_to": "2025Q4"}


def test_control_center_treats_annual_manual_document_as_full_year(db_session):
    company = Company(
        ticker="SBER",
        isin="RU0009029540",
        board="TQBR",
        short_name="SBER",
        full_name="Sberbank",
        is_active=True,
    )
    db_session.add(company)
    db_session.flush()
    db_session.add(
        ReportDocument(
            company_id=company.id,
            report_period="2025",
            reporting_standard="IFRS",
            source_type="manual_upload",
            source_role="financial_statements",
            document_type="financial_statements",
            storage_path="data/raw/manual_uploads/SBER/IFRS/2025/report.pdf",
            file_name="report.pdf",
            file_hash="abc",
            status="validated",
        )
    )
    db_session.commit()

    request = SourcePipelineRunRequest(ticker="SBER", period_from="2025", period_to="2025")
    docs = PipelineControlCenter(db_session)._existing_cached_documents(company, request)
    assert len(docs) == 1
    assert request.period_from == "2025Q1"
    assert request.period_to == "2025Q4"


def test_full_pipeline_job_records_events_and_manual_upload_fallback(db_session, monkeypatch):
    root = runtime_root()

    class FakeSourceReport:
        query = "IRAO"
        warnings = []
        auto_select_allowed = False
        recommended_candidates = [
            SimpleNamespace(
                source_provider="moex_iss",
                verification_scope="identity_only",
                ticker="IRAO",
                matched_name="Inter RAO",
                provenance={"ticker": "IRAO", "board": "TQBR", "name": "Inter RAO", "short_name": "IRAO"},
            )
        ]

        def to_dict(self):
            return {"query": self.query, "recommended_candidates": [], "warnings": []}

    class FakeSourceDiscoveryService:
        def __init__(self, db, root=None):
            self.root = root

        def discover(self, *args, **kwargs):
            return FakeSourceReport()

        def save_report(self, report):
            path = root / "data" / "validation" / "company_source_discovery" / "irao_source_discovery_report.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
            return path

    monkeypatch.setattr("app.services.ui.pipeline_control_center.CompanySourceDiscoveryService", FakeSourceDiscoveryService)
    job = PIPELINE_JOB_STORE.create(FullPipelineRunRequest(ticker="IRAO", period_from="2021Q1", period_to="2021Q4"))
    result = PipelineControlCenter(db_session, root=root).run_full_company_pipeline(job.request, job_id=job.job_id)
    event_payloads = [event.to_dict() for event in PIPELINE_JOB_STORE.get(job.job_id).events]
    assert result.overall_status == "PARTIAL"
    assert any(event["stage"] == "manual_upload_needed" for event in event_payloads)
    assert any(event["stage"] == "financial_ratios" for event in event_payloads)
    assert result.safety["facts_persisted"] is False
    assert result.safety["llm_invoked"] is False
    assert result.safety["manifest_mutated"] is False
    shutil.rmtree(root, ignore_errors=True)


def test_full_pipeline_api_exposes_job_and_events(client):
    response = client.post(
        "/pipeline/full-run",
        json={"ticker": "LKOH", "period_from": "2021Q1", "period_to": "2021Q1", "live_discovery": False},
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    job = client.get(f"/pipeline/jobs/{job_id}").json()
    events = client.get(f"/pipeline/jobs/{job_id}/events").json()
    assert job["job_id"] == job_id
    assert events["job_id"] == job_id
    assert events["events"]
    assert events["result"]["safety"]["facts_persisted"] is False


def test_manual_upload_endpoint_cleans_temp_file_and_preserves_trust_boundary(client, monkeypatch):
    captured = {}

    class FakeManualReportIngestionService:
        def __init__(self, db):
            self.db = db

        def ingest(self, request):
            captured["temp_path"] = Path(request.local_file_path)
            captured["auto_fetch_market_data"] = request.auto_fetch_market_data
            assert captured["temp_path"].exists()
            return SimpleNamespace(
                to_dict=lambda: {
                    "status": "VALIDATION_FAILED",
                    "report_document_id": 10,
                    "duplicate_detected": False,
                    "document_validation_status": "fail",
                    "statement_tables_extracted": 0,
                    "fact_parse_status": "not_invoked",
                    "db_persisted": False,
                    "source_trust_bucket": "manual_upload_unverified",
                    "official_source_verified": False,
                    "source_package_ready_contribution": False,
                }
            )

    monkeypatch.setattr(reports_route, "ManualReportIngestionService", FakeManualReportIngestionService)
    response = client.post(
        "/reports/manual-upload",
        data={"company_ticker": "LKOH", "period": "2021Q4", "reporting_standard": "IFRS"},
        files={"file": ("report.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    payload = response.json()
    assert response.status_code == 200
    assert payload["db_persisted"] is False
    assert payload["official_source_verified"] is False
    assert payload["source_package_ready_contribution"] is False
    assert captured["auto_fetch_market_data"] is True
    assert captured["temp_path"].exists() is False


def test_manual_upload_endpoint_cleans_temp_file_on_ingestion_error(client, monkeypatch):
    captured = {}

    class FailingManualReportIngestionService:
        def __init__(self, db):
            self.db = db

        def ingest(self, request):
            captured["temp_path"] = Path(request.local_file_path)
            assert captured["temp_path"].exists()
            raise RuntimeError("validation exploded")

    monkeypatch.setattr(reports_route, "ManualReportIngestionService", FailingManualReportIngestionService)
    try:
        client.post(
            "/reports/manual-upload",
            data={"company_ticker": "LKOH", "period": "2021Q4", "reporting_standard": "IFRS"},
            files={"file": ("report.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
        )
    except RuntimeError:
        pass
    assert captured["temp_path"].exists() is False


def test_manual_upload_endpoint_rejects_xls(client):
    response = client.post(
        "/reports/manual-upload",
        data={"company_ticker": "LKOH", "period": "2021Q4", "reporting_standard": "IFRS"},
        files={"file": ("legacy.xls", b"not supported", "application/vnd.ms-excel")},
    )
    assert response.status_code == 200
    payload = response.json()
    assert "xls_not_supported_in_v1" in payload["blockers"]
    assert payload["db_persisted"] is False
