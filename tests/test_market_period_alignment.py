import shutil
from pathlib import Path
from uuid import uuid4

import pandas as pd

from app.db.models import Company
from app.services.analysis.llm_payload_builder import LLMPayloadBuilder
from app.services.analysis.result_builder import ResultBuilder
from app.services.market.market_audit import (
    build_market_audit_report,
    market_analysis_from_audit,
    run_market_audit,
    save_cached_candles,
)


def _cache_root() -> Path:
    root = Path("data") / "validation" / "test_market_period_alignment" / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root


def _candles(start: str, days: int = 60) -> pd.DataFrame:
    rows = []
    for index, day in enumerate(pd.bdate_range(start, periods=days)):
        close = 100 + index
        rows.append(
            {
                "date": day.date(),
                "open": close - 1,
                "high": close + 1,
                "low": close - 2,
                "close": close,
                "volume": 1_000_000 + index,
                "value": (1_000_000 + index) * close,
            }
        )
    return pd.DataFrame(rows)


def test_2025_fixture_candles_for_2021_request_are_unaligned():
    report = build_market_audit_report(
        "LKOH",
        "TQBR",
        "2021Q1",
        "2021Q4",
        _candles("2025-01-03"),
        "fixture_candles",
        market_data_mode="fixture",
    )

    assert report["requested_date_range"] == {"from": "2021-01-01", "to": "2021-12-31"}
    assert report["actual_candle_date_range"]["from"] == "2025-01-03"
    assert report["market_period_aligned"] is False
    assert report["coverage"]["coverage_ratio"] == 0.0
    assert report["market_data_source"] == "fixture_candles_unaligned"


def test_unaligned_market_data_adds_warning():
    report = build_market_audit_report(
        "LKOH",
        "TQBR",
        "2021Q1",
        "2021Q4",
        _candles("2025-01-03"),
        "fixture_candles",
        market_data_mode="fixture",
    )

    assert "Market candle date range does not overlap requested analysis period." in report["warnings"]
    assert any("must not be interpreted as 2021 LKOH market analysis" in warning for warning in report["warnings"])


def test_unaligned_indicators_are_not_valid_for_requested_period():
    report = build_market_audit_report(
        "LKOH",
        "TQBR",
        "2021Q1",
        "2021Q4",
        _candles("2025-01-03"),
        "fixture_candles",
        market_data_mode="fixture",
    )
    ma20 = next(item for item in report["technical_indicators"] if item["indicator_code"] == "ma_20")

    assert ma20["calculation_valid_on_available_candles"] is True
    assert ma20["applicable_to_requested_period"] is False
    assert ma20["status"] == "not_applicable_to_requested_period"


def test_aligned_fixture_candles_for_2021_request_are_aligned():
    report = build_market_audit_report(
        "LKOH",
        "TQBR",
        "2021Q1",
        "2021Q4",
        _candles("2021-01-04"),
        "fixture_candles",
        market_data_mode="fixture",
    )

    assert report["market_period_aligned"] is True
    assert report["technical_indicators"][0]["applicable_to_requested_period"] is True


def test_replay_cache_missing_returns_controlled_warning():
    cache_root = _cache_root()
    try:
        report = run_market_audit("2099Q1", "2099Q4", mode="replay-cache", cache_root=cache_root)

        assert report["status"] == "FAIL"
        assert report["market_data_mode"] == "replay_cache"
        assert "Cached market data not found; run live market audit first." in report["warnings"]
    finally:
        shutil.rmtree(cache_root, ignore_errors=True)


def test_replay_cache_reads_cached_candles_without_internet():
    cache_root = _cache_root()
    try:
        save_cached_candles(
            _candles("2021-01-04"), "LKOH", "TQBR", "2021-01-01", "2021-12-31", cache_root=cache_root
        )

        report = run_market_audit("2021Q1", "2021Q4", mode="replay-cache", cache_root=cache_root)

        assert report["status"] == "PARTIAL"
        assert report["market_data_mode"] == "replay_cache"
        assert report["market_data_source"] == "market_cache"
        assert report["market_period_aligned"] is True
        assert report["summary"]["candles_count"] == 60
    finally:
        shutil.rmtree(cache_root, ignore_errors=True)


def test_result_json_contains_market_period_alignment_flag():
    report = build_market_audit_report(
        "LKOH",
        "TQBR",
        "2021Q1",
        "2021Q4",
        _candles("2025-01-03"),
        "fixture_candles",
        market_data_mode="fixture",
    )
    company = Company(id=1, ticker="LKOH", short_name="LKOH", full_name="LKOH", board="TQBR")
    result = ResultBuilder().build(
        company,
        "2021Q1",
        "2021Q4",
        "IFRS",
        [],
        [],
        [],
        market_analysis_from_audit(report),
        {"peer_table": [], "peer_summary": {}, "warnings": []},
        [],
        data_mode="real",
    )

    assert result["market_analysis"]["market_period_aligned"] is False
    assert "market_period_unaligned" in result["data_quality"]["flags"]


def test_llm_payload_warns_when_market_data_unaligned():
    company = Company(id=1, ticker="LKOH", board="TQBR", short_name="LKOH", full_name="LKOH")
    payload = LLMPayloadBuilder().build(
        company,
        "2021Q1",
        "2021Q4",
        "IFRS",
        [],
        {"market_period_aligned": False},
        {},
        [],
        [],
        data_mode="real",
    )

    assert "market_period_unaligned" in payload["data_quality_flags"]
    assert any("must not be interpreted as 2021 LKOH market analysis" in warning for warning in payload["warnings"])


def test_fixture_mode_does_not_use_live_moex(monkeypatch):
    def fail_live_call(*args, **kwargs):
        raise AssertionError("MOEX client should not be used in fixture mode")

    monkeypatch.setattr("app.services.market.market_audit.MoexClient", fail_live_call)

    report = run_market_audit("2021Q1", "2021Q4", mode="fixture")

    assert report["market_data_mode"] == "fixture"
