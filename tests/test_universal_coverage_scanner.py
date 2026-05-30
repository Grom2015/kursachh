from pathlib import Path
from types import SimpleNamespace

from app.db.models import MetricValue, StatementFact
from app.services.coverage.universal_coverage_scanner import CoverageScanRequest, UniversalCoverageScanner
from app.tools import scan_universal_coverage as cli


class SessionContext:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *_args):
        return None


def test_lkoh_tatn_gazp_scan_returns_company_items(db_session):
    report = UniversalCoverageScanner(db_session).scan(
        CoverageScanRequest(
            tickers=["LKOH", "TATN", "GAZP"],
            from_registry=False,
            period_from="2021Q1",
            period_to="2021Q4",
        )
    )

    assert report.scan_mode == "existing_artifacts_only"
    assert report.actions_executed == []
    assert report.artifacts_created is False
    assert [item["ticker"] for item in report.company_items] == ["LKOH", "TATN", "GAZP"]


def test_known_company_blockers_and_coverage_levels_are_reflected(db_session):
    report = UniversalCoverageScanner(db_session).scan(
        CoverageScanRequest(
            tickers=["LKOH", "TATN", "GAZP"],
            from_registry=False,
            period_from="2021Q1",
            period_to="2021Q4",
        )
    )
    by_ticker = {item["ticker"]: item for item in report.company_items}

    assert by_ticker["LKOH"]["coverage_level"] == "FULL_STATEMENT_READY"
    assert by_ticker["TATN"]["coverage_level"] == "SOURCE_BLOCKED"
    assert "missing_fy_report" in by_ticker["TATN"]["blockers"]
    assert by_ticker["GAZP"]["coverage_level"] == "PARSER_BLOCKED"
    assert by_ticker["GAZP"]["main_blocker"] == "image_only_primary_statement_pages"
    assert "image_only_primary_statement_pages" in by_ticker["GAZP"]["blockers"]
    assert "unavailable_valuation_inputs" in by_ticker["GAZP"]["blockers"]


def test_blocker_summary_aggregates_counts(db_session):
    report = UniversalCoverageScanner(db_session).scan(
        CoverageScanRequest(
            tickers=["TATN", "GAZP"],
            from_registry=False,
            period_from="2021Q1",
            period_to="2021Q4",
        )
    )

    summary = {item["blocker_code"]: item for item in report.blocker_summary}
    assert summary["missing_fy_report"]["count"] == 1
    assert summary["missing_fy_report"]["tickers_sample"] == ["TATN"]
    assert summary["image_only_primary_statement_pages"]["tickers_sample"] == ["GAZP"]


def test_roi_recommendation_prefers_statement_blocker_over_valuation(db_session):
    report = UniversalCoverageScanner(db_session).scan(
        CoverageScanRequest(
            tickers=["LKOH", "TATN", "GAZP"],
            from_registry=False,
            period_from="2021Q1",
            period_to="2021Q4",
        )
    )

    assert report.recommended_next_engineering_action is not None
    assert report.recommended_next_engineering_action["blocker_code"] != "unavailable_valuation_inputs"
    assert any(item["blocker_code"] == "unavailable_valuation_inputs" for item in report.recommended_not_to_prioritize)


def test_run_missing_table_extraction_records_action(monkeypatch, db_session):
    from app.services.coverage import universal_coverage_scanner as scanner_module

    monkeypatch.setattr(
        scanner_module.UniversalCoverageScanner,
        "_cached_documents",
        lambda self, company, request: [SimpleNamespace(report_period="2021Q1")],
    )
    monkeypatch.setattr(
        scanner_module.UniversalCoverageScanner,
        "_statement_table_report",
        lambda self, ticker, request: {},
    )
    monkeypatch.setattr(
        scanner_module.UniversalCoverageScanner,
        "_run_table_extraction",
        lambda self, ticker, request: setattr(self, "artifacts_created", True)
        or {
            "statement_tables_count": 0,
            "tables_extracted": 0,
            "required_statement_tables_found": False,
            "warnings": [],
        },
    )
    report = UniversalCoverageScanner(db_session).scan(
        CoverageScanRequest(
            tickers=["LKOH"],
            from_registry=False,
            period_from="2021Q1",
            period_to="2021Q4",
            run_missing_table_extraction=True,
        )
    )

    assert report.scan_mode == "replay_cache_with_table_extraction"
    assert report.actions_executed == ["statement_table_extraction"]
    assert report.artifacts_created is True


def test_scanner_does_not_persist_facts_or_metrics(db_session):
    before_facts = db_session.query(StatementFact).count()
    before_metrics = db_session.query(MetricValue).count()

    UniversalCoverageScanner(db_session).scan(
        CoverageScanRequest(
            tickers=["LKOH", "GAZP"],
            from_registry=False,
            period_from="2021Q1",
            period_to="2021Q4",
            allow_text_fallback_semantic_gate=True,
        )
    )

    assert db_session.query(StatementFact).count() == before_facts
    assert db_session.query(MetricValue).count() == before_metrics


def test_cli_writes_json_report(monkeypatch, db_session):
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "SessionLocal", lambda: SessionContext(db_session))
    runtime_root = Path("tests/runtime_universal_coverage_scanner")
    runtime_root.mkdir(parents=True, exist_ok=True)

    original_init = cli.UniversalCoverageScanner.__init__

    def patched_init(self, db, root=None):
        original_init(self, db, root=runtime_root)

    monkeypatch.setattr(cli.UniversalCoverageScanner, "__init__", patched_init)
    report, path = cli.scan_universal_coverage(
        "2021Q1",
        "2021Q4",
        tickers=["LKOH", "TATN", "GAZP"],
        from_registry=False,
        replay_cache=True,
    )

    assert Path(path).exists()
    assert report["scan_mode"] == "existing_artifacts_only"
    assert report["artifacts_created"] is False
    assert report["companies_scanned"] == 3
    assert "recommended_next_engineering_action" in report
    assert "recommended_not_to_prioritize" in report


def test_from_registry_scan_can_include_broader_universe(db_session):
    report = UniversalCoverageScanner(db_session).scan(
        CoverageScanRequest(
            from_registry=True,
            limit=50,
            period_from="2021Q1",
            period_to="2021Q4",
        )
    )

    assert report.companies_scanned > 3


def test_scanner_continues_when_one_company_fails(monkeypatch, db_session):
    original = UniversalCoverageScanner._company_item

    def fail_for_tatn(self, company, request):
        if company.ticker == "TATN":
            raise RuntimeError("boom")
        return original(self, company, request)

    monkeypatch.setattr(UniversalCoverageScanner, "_company_item", fail_for_tatn)
    report = UniversalCoverageScanner(db_session).scan(
        CoverageScanRequest(
            tickers=["LKOH", "TATN"],
            from_registry=False,
            period_from="2021Q1",
            period_to="2021Q4",
        )
    )

    by_ticker = {item["ticker"]: item for item in report.company_items}
    assert by_ticker["TATN"]["coverage_level"] == "UNSUPPORTED"
    assert by_ticker["TATN"]["main_blocker"] == "scanner_company_failed"
    assert by_ticker["LKOH"]["coverage_level"] == "FULL_STATEMENT_READY"
