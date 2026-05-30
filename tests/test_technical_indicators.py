import pandas as pd

from app.core.config import get_settings
from app.services.market.technical_indicators import calculate_technical_indicators


def fixture_candles():
    return pd.read_csv(get_settings().root_dir / "data" / "fixtures" / "lkoh_candles.csv")


def test_ma20_matches_tail_mean():
    df = fixture_candles()
    result = calculate_technical_indicators(df)
    assert result["indicators"]["ma_20"]["value"] == df["close"].tail(20).mean()


def test_rsi14_in_range():
    result = calculate_technical_indicators(fixture_candles())
    rsi = result["indicators"]["rsi_14"]["value"]
    assert 0 <= rsi <= 100


def test_max_drawdown_matches_formula():
    df = fixture_candles()
    result = calculate_technical_indicators(df)
    expected = ((df["close"] / df["close"].cummax()) - 1).min()
    assert result["indicators"]["max_drawdown"]["value"] == expected


def test_not_enough_data_returns_warning():
    result = calculate_technical_indicators(fixture_candles().head(5))
    assert result["indicators"]["ma_20"]["quality_flag"] == "missing"
    assert result["warnings"]


def test_empty_dataframe_returns_warning():
    result = calculate_technical_indicators(pd.DataFrame())
    assert result["warnings"] == ["No market candles available"]
    assert result["indicators"] == {}


def test_max_drawdown_never_positive():
    result = calculate_technical_indicators(fixture_candles())
    assert result["indicators"]["max_drawdown"]["value"] <= 0
