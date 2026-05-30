import shutil
from pathlib import Path
from uuid import uuid4

from app.services.market.market_audit import run_market_audit


def cache_root() -> Path:
    root = Path("data") / "validation" / "test_gazp_market_audit" / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_gazp_market_replay_cache_missing_returns_controlled_fail():
    root = cache_root()
    try:
        report = run_market_audit("2021Q1", "2021Q4", ticker="GAZP", mode="replay-cache", cache_root=root)

        assert report["company"] == "GAZP"
        assert report["status"] == "FAIL"
        assert report["summary"]["candles_count"] == 0
        assert "Cached market data not found; run live market audit first." in report["warnings"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_gazp_fixture_mode_does_not_claim_live_market_data():
    report = run_market_audit("2021Q1", "2021Q4", ticker="GAZP", mode="fixture")

    assert report["company"] == "GAZP"
    assert report["market_data_mode"] == "fixture"
    assert report["market_data_source"] != "MOEX ISS"
