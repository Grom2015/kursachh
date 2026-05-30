import pandas as pd


def _missing(reason: str) -> dict:
    return {"value": None, "quality_flag": "missing", "warnings": [reason]}


def _value(value: float | None) -> dict:
    return {"value": None if value is None else float(value), "quality_flag": "exact", "warnings": []}


def calculate_technical_indicators(df: pd.DataFrame) -> dict:
    if df.empty or "close" not in df.columns:
        return {"warnings": ["No market candles available"], "indicators": {}}
    data = df.copy()
    data["close"] = pd.to_numeric(data["close"], errors="coerce")
    data["volume"] = pd.to_numeric(data.get("volume", 0), errors="coerce")
    data["value"] = pd.to_numeric(data.get("value", 0), errors="coerce")
    close = data["close"].dropna()
    returns = close.pct_change().dropna()
    indicators: dict[str, dict] = {}
    warnings: list[str] = []

    indicators["daily_return"] = _value(returns.iloc[-1] if not returns.empty else None)
    for window in [20, 60]:
        key = f"volatility_{window}d"
        if len(returns) < window:
            indicators[key] = _missing(f"Not enough candles for {key}")
            warnings.extend(indicators[key]["warnings"])
        else:
            indicators[key] = _value(returns.tail(window).std() * (252**0.5))
    for window in [20, 50, 200]:
        key = f"ma_{window}"
        if len(close) < window:
            indicators[key] = _missing(f"Not enough candles for {key}")
            warnings.extend(indicators[key]["warnings"])
        else:
            indicators[key] = _value(close.tail(window).mean())
    indicators["rsi_14"] = _rsi(close, 14)
    if indicators["rsi_14"]["warnings"]:
        warnings.extend(indicators["rsi_14"]["warnings"])
    indicators["average_daily_volume"] = _value(data["volume"].tail(20).mean())
    indicators["average_daily_turnover"] = _value(data["value"].tail(20).mean())
    indicators["max_drawdown"] = _value(((close / close.cummax()) - 1).min())
    tail = close.tail(min(20, len(close)))
    indicators["support_level"] = _value(tail.min())
    indicators["resistance_level"] = _value(tail.max())
    return {"warnings": warnings, "indicators": indicators}


def _rsi(close: pd.Series, window: int = 14) -> dict:
    if len(close) <= window:
        return _missing("Not enough candles for rsi_14")
    delta = close.diff().dropna()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.rolling(window).mean().iloc[-1]
    avg_loss = losses.rolling(window).mean().iloc[-1]
    if pd.isna(avg_gain) or pd.isna(avg_loss):
        return _missing("Not enough candles for rsi_14")
    if avg_loss == 0:
        return _value(100.0)
    rs = avg_gain / avg_loss
    return _value(100 - (100 / (1 + rs)))

