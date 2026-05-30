import shutil
from pathlib import Path
from uuid import uuid4

import pandas as pd

from app.services.market.market_audit import (
    build_market_audit_report,
    date_to_warnings,
    period_date_range,
    run_market_technical_report,
    save_cached_candles,
)
from app.tools import build_market_technical_report as cli


def _cache_root() -> Path:
    root = Path("data") / "validation" / "test_market_technical_report" / uuid4().hex
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
    df = pd.DataFrame(rows)
    df.attrs["provider_columns"] = ["begin", "open", "high", "low", "close", "volume", "value"]
    df.attrs["provider_endpoint"] = "https://iss.moex.com/iss/mock/candles.json"
    return df


def test_quarter_periods_map_to_inclusive_dates():
    assert period_date_range("2021Q1", "2021Q1") == ("2021-01-01", "2021-03-31")
    assert period_date_range("2021Q2", "2021Q2") == ("2021-04-01", "2021-06-30")
    assert period_date_range("2021Q3", "2021Q3") == ("2021-07-01", "2021-09-30")
    assert period_date_range("2021Q4", "2021Q4") == ("2021-10-01", "2021-12-31")
    assert period_date_range("2021Q1", "2021Q4") == ("2021-01-01", "2021-12-31")


def test_date_to_included_or_controlled_warning():
    report = build_market_audit_report("LKOH", "TQBR", "2021Q4", "2021Q4", _candles("2021-10-01", 66), "MOEX ISS")
    assert report["actual_candle_date_range"]["to"] == "2021-12-31"
    assert date_to_warnings(report) == []

    omitted = build_market_audit_report("LKOH", "TQBR", "2021Q4", "2021Q4", _candles("2021-10-01", 65), "MOEX ISS")
    assert "date_to_not_present_if_non_trading_or_provider_omitted" in date_to_warnings(omitted)


def test_market_technical_report_has_provider_units_latest_summary_and_spread_missing_not_fail():
    report = run_market_technical_report(
        "LKOH",
        "2021Q1",
        "2021Q4",
        mode="mocked",
        candles_df=_candles("2021-01-04", 255),
    )

    assert report["status"] == "PASS"
    assert report["provider"] == "moex_iss"
    assert report["provider_columns"] == ["begin", "open", "high", "low", "close", "volume", "value"]
    assert report["volume_unit"] == "unknown_or_lots_or_securities"
    assert report["turnover_unit"] == "RUB"
    spread = next(item for item in report["liquidity_metrics"] if item["metric_code"] == "bid_ask_spread")
    assert spread["status"] == "missing"
    assert spread["reason"] == "requires_orderbook_or_bid_ask_data"
    assert report["latest_summary"]["close"] is not None
    assert report["latest_summary"]["ma_20"] is not None
    assert report["latest_summary"]["ma_50"] is not None
    assert report["latest_summary"]["rsi_14"] is not None
    assert report["latest_summary"]["support_level"] is not None
    assert report["latest_summary"]["resistance_level"] is not None


def test_replay_cache_and_missing_cache_modes_are_controlled():
    cache_root = _cache_root()
    try:
        save_cached_candles(_candles("2021-01-04", 60), "LKOH", "TQBR", "2021-01-01", "2021-12-31", cache_root)
        report = run_market_technical_report("LKOH", "2021Q1", "2021Q4", mode="replay-cache", cache_root=cache_root)
        assert report["summary"]["candles_count"] == 60
        assert report["market_data_mode"] == "replay_cache"

        missing = run_market_technical_report("GAZP", "2021Q1", "2021Q4", mode="replay-cache", cache_root=cache_root)
        assert missing["status"] == "FAIL"
        assert "Cached market data not found; run live market audit first." in missing["warnings"]
    finally:
        shutil.rmtree(cache_root, ignore_errors=True)


def test_live_mode_calls_client_and_writes_cache(monkeypatch):
    cache_root = _cache_root()

    class FakeClient:
        def get_security_candles(self, ticker, board, from_date, to_date):
            assert (ticker, board, from_date, to_date) == ("LKOH", "TQBR", "2021-01-01", "2021-12-31")
            return _candles("2021-01-04", 60)

    monkeypatch.setattr("app.services.market.market_audit.MoexClient", FakeClient)
    try:
        report = run_market_technical_report("LKOH", "2021Q1", "2021Q4", mode="live", cache_root=cache_root)
        assert report["summary"]["candles_count"] == 60
        assert (cache_root / "LKOH" / "TQBR" / "2021-01-01_2021-12-31_candles.csv").exists()
    finally:
        shutil.rmtree(cache_root, ignore_errors=True)


def test_market_technical_cli_writes_json(monkeypatch, capsys):
    report = run_market_technical_report("LKOH", "2021Q1", "2021Q4", mode="mocked", candles_df=_candles("2021-01-04", 60))
    path = Path("data") / "validation" / "test_market_technical_report" / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(cli, "run_market_technical_report", lambda **_kwargs: report)
    monkeypatch.setattr(cli, "save_market_technical_report", lambda _report: path)

    built, built_path = cli.build_market_technical_report("LKOH", "2021Q1", "2021Q4")
    assert built["ticker"] == "LKOH"
    assert built_path == path

    assert cli.main(["LKOH", "2021Q1", "2021Q4", "--json-only"]) == 0
    assert "report.json" in capsys.readouterr().out


def teardown_module():
    shutil.rmtree(Path("data") / "validation" / "test_market_technical_report", ignore_errors=True)
