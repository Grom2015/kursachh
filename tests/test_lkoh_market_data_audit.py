import pandas as pd

from app.db.models import Company
from app.services.analysis.result_builder import ResultBuilder
from app.services.market.market_audit import build_market_audit_report, market_analysis_from_audit


def _candles(days=60):
    dates = pd.bdate_range("2021-01-01", periods=days)
    rows = []
    for index, day in enumerate(dates):
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


def test_market_audit_report_created_from_mock_candles():
    report = build_market_audit_report("LKOH", "TQBR", "2021Q1", "2021Q4", _candles(), "mocked")
    assert report["ticker"] == "LKOH"
    assert report["summary"]["candles_count"] == 60
    assert report["status"] == "PARTIAL"


def test_ma20_valid_when_enough_observations():
    report = build_market_audit_report("LKOH", "TQBR", "2021Q1", "2021Q4", _candles(20), "mocked")
    ma20 = next(item for item in report["technical_indicators"] if item["indicator_code"] == "ma_20")
    assert ma20["status"] == "valid"


def test_ma200_missing_when_less_than_200_observations():
    report = build_market_audit_report("LKOH", "TQBR", "2021Q1", "2021Q4", _candles(60), "mocked")
    ma200 = next(item for item in report["technical_indicators"] if item["indicator_code"] == "ma_200")
    assert ma200["status"] == "missing"


def test_rsi14_in_range():
    report = build_market_audit_report("LKOH", "TQBR", "2021Q1", "2021Q4", _candles(60), "mocked")
    rsi = next(item for item in report["technical_indicators"] if item["indicator_code"] == "rsi_14")
    assert rsi["status"] == "valid"
    assert 0 <= rsi["value"] <= 100


def test_max_drawdown_not_positive():
    report = build_market_audit_report("LKOH", "TQBR", "2021Q1", "2021Q4", _candles(60), "mocked")
    drawdown = next(item for item in report["technical_indicators"] if item["indicator_code"] == "max_drawdown")
    assert drawdown["value"] <= 0


def test_average_daily_volume_calculated():
    report = build_market_audit_report("LKOH", "TQBR", "2021Q1", "2021Q4", _candles(60), "mocked")
    volume = next(item for item in report["liquidity_metrics"] if item["metric_code"] == "average_daily_volume")
    assert volume["status"] == "valid"
    assert volume["value"] > 0


def test_bid_ask_spread_missing_without_bid_ask():
    report = build_market_audit_report("LKOH", "TQBR", "2021Q1", "2021Q4", _candles(60), "mocked")
    spread = next(item for item in report["liquidity_metrics"] if item["metric_code"] == "bid_ask_spread")
    assert spread["status"] == "missing"
    assert spread["missing_reason"] == "requires_orderbook_or_bid_ask_data"


def test_valuation_inputs_missing_without_market_cap_shares_dividends():
    report = build_market_audit_report("LKOH", "TQBR", "2021Q1", "2021Q4", _candles(60), "mocked")
    assert all(item["status"] == "missing" for item in report["valuation_inputs"])
    assert report["summary"]["valuation_inputs_available"] is False


def test_market_analysis_result_json_contains_warnings_and_flags():
    report = build_market_audit_report("LKOH", "TQBR", "2021Q1", "2021Q4", _candles(60), "mocked")
    market_analysis = market_analysis_from_audit(report)
    company = Company(id=1, ticker="LKOH", short_name="LKOH", full_name="ПАО ЛУКОЙЛ", board="TQBR")
    result = ResultBuilder().build(
        company,
        "2021Q1",
        "2021Q4",
        "IFRS",
        [],
        [],
        [],
        market_analysis,
        {"peer_table": [], "peer_summary": {}, "warnings": []},
        [],
        data_mode="real",
    )
    assert result["market_analysis"]["warnings"]
    assert "spread_unavailable" in result["data_quality"]["flags"]
    assert "valuation_inputs_missing" in result["data_quality"]["flags"]
    assert "liquidity_limited_to_volume_turnover" in result["data_quality"]["flags"]


def test_market_audit_has_no_internet_dependency():
    report = build_market_audit_report("LKOH", "TQBR", "2021Q1", "2021Q4", _candles(30), "mocked")
    assert report["market_data_source"] == "mocked"
