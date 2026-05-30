import json
import shutil
from pathlib import Path

from app.db.models import MetricValue, StatementFact
from app.services.demo.demo_pipeline_orchestrator import DemoPipelineOrchestrator, DemoPipelineRequest
from app.tools import run_demo_pipeline as cli


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _runtime_root() -> Path:
    root = Path("tests/runtime_demo_pipeline")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    coverage = {
        "company_items": [
            _coverage_item("LKOH", "FULL_STATEMENT_READY", None, "TABLES_READY", "FACTS_READY", "AUTO_READY"),
            _coverage_item("TATN", "SOURCE_BLOCKED", "missing_fy_report", "TABLES_PARTIAL", "FACTS_PARTIAL", "AUTO_PARTIAL"),
            _coverage_item(
                "GAZP",
                "PARSER_BLOCKED",
                "image_only_primary_statement_pages",
                "IMAGE_ONLY_PRIMARY_STATEMENTS",
                "FACTS_READY",
                "AUTO_PARTIAL",
            ),
        ]
    }
    _write_json(root / "data" / "validation" / "coverage" / "moex_ifrs_2021_universal_coverage_scan.json", coverage)
    _write_json(
        root / "data" / "validation" / "providers" / "provider_feasibility_scan.json",
        {
            "providers_checked": 10,
            "summary": {"production_ready_count": 0},
            "best_candidates_by_data_type": {
                "IFRS report discovery": "E-Disclosure / Interfax",
                "RAS financial statements": "FNS GIR BO",
                "company identity": "MOEX ISS",
                "valuation inputs": "Cbonds / RU Data",
            },
            "provider_items": [
                {"provider_name": "MOEX ISS", "role_in_architecture": ["company_identity_provider", "market_data_provider"]},
                {"provider_name": "E-Disclosure / Interfax", "role_in_architecture": ["primary_report_source"]},
            ],
        },
    )
    _write_json(
        root / "data" / "validation" / "providers" / "edisclosure_proof_of_access.json",
        {"access_status": "requires_auth"},
    )
    _write_json(
        root / "data" / "validation" / "LKOH" / "2021Q1_2021Q4_statement_table_extraction.json",
        {
            "statement_tables_count": 66,
            "balance_sheet_tables_count": 24,
            "income_statement_tables_count": 15,
            "cash_flow_tables_count": 4,
        },
    )
    _write_json(
        root / "data" / "validation" / "LKOH" / "2021Q1_2021Q4_dataframe_statement_fact_parse.json",
        {"canonical_fact_candidates": 40},
    )
    _write_json(
        root / "data" / "validation" / "LKOH" / "2021Q1_2021Q4_dataframe_vs_existing_fact_comparison.json",
        {"matched_count": 40, "conflict_count": 0, "dataframe_trust_buckets": {"text_table_fallback_semantic_gate": 40}},
    )
    _write_json(
        root / "data" / "validation" / "LKOH" / "2021Q4_manual_report_ingestion.json",
        {
            "source_trust_bucket": "manual_upload_validated",
            "official_source_verified": False,
            "source_package_ready_contribution": False,
            "statement_tables_extracted": 19,
        },
    )
    _write_json(
        root / "data" / "validation" / "LKOH" / "2021Q1_2021Q4_financial_ratios.json",
        {
            "summary": {"calculated_count": 5, "missing_count": 22, "unsupported_count": 4, "blocked_count": 9},
            "metrics": [
                {
                    "metric_code": "net_margin",
                    "metric_name": "Net Margin",
                    "period": "2021Q4",
                    "value": 0.12,
                    "status": "calculated",
                }
            ],
        },
    )
    _write_json(
        root / "data" / "validation" / "LKOH" / "2021Q1_2021Q4_llm_analysis_payload.json",
        {"company": {"ticker": "LKOH"}},
    )
    _write_json(
        root / "data" / "validation" / "PEERS" / "LKOH_2021Q1_2021Q4_peer_analysis_report.json",
        {
            "peer_selection_status": "ready",
            "comparison_data_status": "partial",
            "comparison_readiness": "PARTIAL",
            "valuation_status": "unavailable",
            "summary": {"peer_count": 5, "metrics_compared_count": 0},
        },
    )
    return root


def _coverage_item(
    ticker: str,
    coverage_level: str,
    main_blocker: str | None,
    table_status: str,
    fact_status: str,
    statement_status: str,
) -> dict:
    blockers = ["unavailable_valuation_inputs"]
    if main_blocker:
        blockers.insert(0, main_blocker)
    return {
        "ticker": ticker,
        "company_name": ticker,
        "coverage_level": coverage_level,
        "main_blocker": main_blocker,
        "identity_status": "RESOLVED",
        "source_discovery_status": "READY",
        "report_discovery_status": "READY" if ticker != "TATN" else "PARTIAL",
        "documents_status": "CACHED_VALIDATED" if ticker != "TATN" else "CACHED_PARTIAL",
        "table_extraction_status": table_status,
        "fact_extraction_status": fact_status,
        "metric_scope_statuses": {"statement_based_financials": statement_status, "valuation_metrics": "UNAVAILABLE"},
        "supported_outputs": ["official_document_analysis"] if ticker == "LKOH" else [],
        "unsupported_outputs": ["valuation_metrics"],
        "recommended_next_action": "Review blockers.",
        "counts": {"cached_documents": 4, "statement_tables": 1, "canonical_fact_candidates": 0},
        "blockers": blockers,
    }


def test_demo_pipeline_generates_json_and_markdown_reports():
    root = _runtime_root()
    orchestrator = DemoPipelineOrchestrator(root=root)
    report = orchestrator.build_report(
        DemoPipelineRequest(tickers=["LKOH", "TATN", "GAZP"], period_from="2021Q1", period_to="2021Q4")
    )
    paths = orchestrator.save_report(report)

    assert Path(paths["json"]).exists()
    assert Path(paths["md"]).exists()
    assert Path(paths["html"]).exists()
    saved = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    markdown = Path(paths["md"]).read_text(encoding="utf-8")
    html = Path(paths["html"]).read_text(encoding="utf-8")
    assert saved["company_results"][0]["ticker"] == "LKOH"
    assert "# Demo Pipeline Report" in markdown
    assert "does not claim universal support" in markdown.lower()
    assert "READY/PARTIAL/BLOCKED" in html
    assert "не вызывает LLM" in html


def test_demo_company_statuses_and_blockers_are_mapped():
    root = _runtime_root()
    report = DemoPipelineOrchestrator(root=root).build_report(
        DemoPipelineRequest(tickers=["LKOH", "TATN", "GAZP"], period_from="2021Q1", period_to="2021Q4")
    )
    by_ticker = {item["ticker"]: item for item in report.company_results}

    assert by_ticker["LKOH"]["main_status"] == "READY"
    assert by_ticker["LKOH"]["coverage_level"] == "FULL_STATEMENT_READY"
    assert by_ticker["TATN"]["main_status"] == "BLOCKED"
    assert by_ticker["TATN"]["main_blocker"] == "missing_fy_report"
    assert by_ticker["GAZP"]["main_status"] == "BLOCKED"
    assert by_ticker["GAZP"]["main_blocker"] == "image_only_primary_statement_pages"


def test_manual_and_provider_summaries_are_conservative():
    root = _runtime_root()
    report = DemoPipelineOrchestrator(root=root).build_report(
        DemoPipelineRequest(tickers=["LKOH", "TATN", "GAZP"], period_from="2021Q1", period_to="2021Q4")
    )
    lkoh = next(item for item in report.company_results if item["ticker"] == "LKOH")

    assert report.manual_fallback_summary["available"] is True
    assert "manual_document_analysis" in lkoh["available_outputs"]
    assert any("official_source_verified=False" in note for note in lkoh["trust_notes"])
    assert report.provider_strategy_summary["production_ready_providers"] == 0
    assert report.provider_strategy_summary["market_identity_candidate"] == "MOEX ISS"
    assert lkoh["financial_ratios_summary"]["available"] is True
    assert lkoh["key_numbers"]["financial_ratios_calculated_count"] == 5
    assert lkoh["financial_ratios_summary"]["sample_calculated_metrics"][0]["metric_code"] == "net_margin"
    assert lkoh["llm_payload_available"] is True
    assert lkoh["evidence_paths"]["llm_analysis_payload"] is not None
    assert lkoh["peer_analysis_summary"]["available"] is True
    assert lkoh["peer_analysis_summary"]["peer_selection_status"] == "ready"
    assert "peer_selection" in lkoh["available_outputs"]


def test_demo_reports_missing_ratios_as_unavailable_without_generating_them():
    root = _runtime_root()
    (root / "data" / "validation" / "LKOH" / "2021Q1_2021Q4_financial_ratios.json").unlink()

    report = DemoPipelineOrchestrator(root=root).build_report(
        DemoPipelineRequest(tickers=["LKOH"], period_from="2021Q1", period_to="2021Q4")
    )
    lkoh = report.company_results[0]

    assert lkoh["financial_ratios_summary"] == {"available": False}
    assert lkoh["key_numbers"]["financial_ratios_calculated_count"] == 0
    assert lkoh["llm_payload_available"] is True


def test_demo_orchestrator_does_not_mutate_db_or_manifests(db_session):
    root = _runtime_root()
    manifests = {path: path.read_bytes() for path in Path("data/manifests").glob("**/*") if path.is_file()}
    before_facts = db_session.query(StatementFact).count()
    before_metrics = db_session.query(MetricValue).count()

    DemoPipelineOrchestrator(root=root).build_report(
        DemoPipelineRequest(tickers=["LKOH", "TATN", "GAZP"], period_from="2021Q1", period_to="2021Q4")
    )

    assert db_session.query(StatementFact).count() == before_facts
    assert db_session.query(MetricValue).count() == before_metrics
    for path, content in manifests.items():
        assert path.read_bytes() == content


def test_demo_cli_writes_reports(monkeypatch):
    root = _runtime_root()
    original_init = cli.DemoPipelineOrchestrator.__init__

    def patched_init(self, root=None):
        original_init(self, root=root or root_path)

    root_path = root
    monkeypatch.setattr(cli.DemoPipelineOrchestrator, "__init__", patched_init)

    report, paths = cli.run_demo_pipeline(["LKOH", "TATN", "GAZP"], "2021Q1", "2021Q4")

    assert report["company_results"][0]["main_status"] == "READY"
    assert Path(paths["json"]).exists()
    assert Path(paths["md"]).exists()
    assert Path(paths["html"]).exists()


def teardown_module():
    shutil.rmtree("tests/runtime_demo_pipeline", ignore_errors=True)
