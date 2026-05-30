import json
import shutil
from pathlib import Path

from sqlalchemy import select

from app.db.models import Company, MetricValue, StatementFact
from app.services.peers.strict_peer_analysis import PeerAnalysisRequest, StrictPeerAnalysisService
from app.tools import build_peer_analysis_report as cli


def _runtime_root(name: str) -> Path:
    root = Path("tests/runtime_strict_peer_analysis") / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_ratios(root: Path, ticker: str, calculated_count: int = 8) -> None:
    metrics = []
    for code in [
        "roe",
        "roa",
        "operating_margin",
        "net_margin",
        "current_ratio",
        "fcf_margin",
        "debt_to_equity",
        "revenue_growth",
    ][:calculated_count]:
        metrics.append(
            {
                "metric_code": code,
                "metric_name": code,
                "period": "2021Q4",
                "value": 0.1,
                "display_value": "10.0%",
                "unit": "ratio",
                "status": "calculated",
                "formula": "fixture-free test payload",
                "inputs": {},
                "warnings": [],
                "methodology_notes": [],
            }
        )
    path = root / "data" / "validation" / ticker / "2021Q1_2021Q4_financial_ratios.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "company_ticker": ticker,
                "period_from": "2021Q1",
                "period_to": "2021Q4",
                "reporting_standard": "IFRS",
                "summary": {
                    "calculated_count": calculated_count,
                    "missing_count": 0,
                    "unsupported_count": 0,
                    "blocked_count": 0,
                },
                "metrics": metrics,
            }
        ),
        encoding="utf-8",
    )


def _service(db_session, root: Path) -> StrictPeerAnalysisService:
    return StrictPeerAnalysisService(db_session, root=root)


def test_lkoh_peer_selection_preflight_and_missing_peer_rows(db_session):
    root = _runtime_root("missing_peers")
    _write_ratios(root, "LKOH")

    report = _service(db_session, root).build(PeerAnalysisRequest(ticker="LKOH", period_from="2021Q1", period_to="2021Q4"))
    data = report.to_dict()

    assert [peer["ticker"] for peer in data["selected_peers"]] == ["ROSN", "SIBN", "TATN", "NVTK", "GAZP"]
    assert data["peer_selection_status"] == "ready"
    assert data["comparison_data_status"] == "partial"
    assert data["valuation_status"] == "unavailable"
    assert data["comparison_readiness"] != "READY_WITH_LIMITATIONS"
    assert data["ratio_report_availability"][0]["status"] == "available"
    assert all(item["status"] == "missing_report" for item in data["ratio_report_availability"][1:])
    peer_row = next(row for row in data["comparison_table"] if row["ticker"] == "ROSN")
    assert peer_row["row_status"] == "metrics_unavailable"
    assert peer_row["metrics"]["net_margin"]["status"] == "report_missing"
    assert peer_row["metrics"]["pe_ratio"]["reason"] == "market_cap_or_price_inputs_missing"
    assert peer_row["metrics"]["ev_to_ebitda"]["reason"] == "enterprise_value_or_explicit_ebitda_missing"
    assert "peer_ratios_report_missing" in data["blockers"]
    assert data["recommended_next_actions"][0]["action"] == "calculate_missing_peer_ratios"
    assert data["recommended_next_actions"][0]["tickers"] == ["ROSN", "SIBN", "TATN", "NVTK", "GAZP"]


def test_ready_with_limitations_requires_two_peer_ratio_reports(db_session):
    root = _runtime_root("ready_with_limits")
    _write_ratios(root, "LKOH")
    _write_ratios(root, "ROSN")
    _write_ratios(root, "SIBN")

    data = _service(db_session, root).build(
        PeerAnalysisRequest(ticker="LKOH", period_from="2021Q1", period_to="2021Q4")
    ).to_dict()

    assert data["comparison_data_status"] == "partial"
    assert data["comparison_readiness"] == "READY_WITH_LIMITATIONS"
    assert data["summary"]["available_peer_count"] == 2
    assert data["summary"]["metrics_compared_count"] > 0


def test_target_ratios_missing_is_not_ready(db_session):
    root = _runtime_root("target_missing")
    data = _service(db_session, root).build(
        PeerAnalysisRequest(ticker="LKOH", period_from="2021Q1", period_to="2021Q4")
    ).to_dict()

    assert data["comparison_data_status"] == "not_ready"
    assert data["comparison_readiness"] == "NOT_READY"
    assert "target_ratios_report_missing" in data["blockers"]


def test_low_coverage_and_same_sector_fallback(db_session):
    root = _runtime_root("low_coverage")
    target = Company(ticker="TESTP", board="TQBR", short_name="TESTP", full_name="TESTP", sector="oil_and_gas")
    db_session.add(target)
    db_session.commit()
    _write_ratios(root, "TESTP", calculated_count=1)

    data = _service(db_session, root).build(
        PeerAnalysisRequest(ticker="TESTP", period_from="2021Q1", period_to="2021Q4", max_peers=3)
    ).to_dict()

    assert data["peer_selection_status"] == "ready"
    assert len(data["selected_peers"]) == 3
    assert data["ratio_report_availability"][0]["status"] == "available_but_low_coverage"
    assert data["comparison_table"][0]["row_status"] == "low_coverage"


def test_missing_sector_or_company_is_not_ready(db_session):
    root = _runtime_root("missing_sector")
    no_sector = Company(ticker="NOSEC", board="TQBR", short_name="NOSEC", full_name="NOSEC")
    db_session.add(no_sector)
    db_session.commit()
    data = _service(db_session, root).build(
        PeerAnalysisRequest(ticker="NOSEC", period_from="2021Q1", period_to="2021Q4")
    ).to_dict()
    assert data["peer_selection_status"] == "not_ready"

    missing = _service(db_session, root).build(
        PeerAnalysisRequest(ticker="MISS", period_from="2021Q1", period_to="2021Q4")
    ).to_dict()
    assert missing["comparison_readiness"] == "NOT_READY"
    assert missing["blockers"] == ["target_company_not_found"]


def test_save_report_and_no_mutation(db_session):
    root = _runtime_root("save")
    _write_ratios(root, "LKOH")
    manifests = {path: path.read_bytes() for path in Path("data/manifests").glob("**/*") if path.is_file()}
    before_facts = db_session.query(StatementFact).count()
    before_metrics = db_session.query(MetricValue).count()

    service = _service(db_session, root)
    report = service.build(PeerAnalysisRequest(ticker="LKOH", period_from="2021Q1", period_to="2021Q4"))
    path = service.save_report(report)

    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["target_ticker"] == "LKOH"
    assert db_session.query(StatementFact).count() == before_facts
    assert db_session.query(MetricValue).count() == before_metrics
    for path, content in manifests.items():
        assert path.read_bytes() == content


def test_cli_writes_json(monkeypatch, db_session, capsys):
    root = _runtime_root("cli")
    _write_ratios(root, "LKOH")
    monkeypatch.setattr(cli, "init_db", lambda: None)

    class SessionContext:
        def __enter__(self):
            return db_session

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(cli, "SessionLocal", lambda: SessionContext())
    original_init = cli.StrictPeerAnalysisService.__init__

    def patched_init(self, db, root=None):
        original_init(self, db, root=root_path)

    root_path = root
    monkeypatch.setattr(cli.StrictPeerAnalysisService, "__init__", patched_init)

    report, path = cli.build_peer_analysis_report("LKOH", "2021Q1", "2021Q4")
    assert report["target_ticker"] == "LKOH"
    assert path.exists()
    assert cli.main(["LKOH", "2021Q1", "2021Q4", "--json-only"]) == 0
    assert "peer_analysis_report.json" in capsys.readouterr().out


def test_no_fixture_peer_metrics_are_used(db_session):
    root = _runtime_root("no_fixture")
    _write_ratios(root, "LKOH")
    report = _service(db_session, root).build(PeerAnalysisRequest(ticker="LKOH", period_from="2021Q1", period_to="2021Q4"))

    assert all("fixture" not in json.dumps(row).lower() for row in report.comparison_table)
    assert db_session.scalar(select(Company).where(Company.ticker == "LKOH")) is not None


def teardown_module():
    shutil.rmtree("tests/runtime_strict_peer_analysis", ignore_errors=True)
