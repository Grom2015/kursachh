import json
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.config import get_settings
from app.services.market.moex_client import MoexClient, MoexClientError
from app.services.market.technical_indicators import calculate_technical_indicators
from app.services.periods import period_key

TECHNICAL_REQUIREMENTS = {
    "daily_return": 2,
    "volatility_20d": 20,
    "volatility_60d": 60,
    "ma_20": 20,
    "ma_50": 50,
    "ma_200": 200,
    "rsi_14": 15,
    "max_drawdown": 2,
    "support_level": 20,
    "resistance_level": 20,
}


def run_market_audit(
    period_from: str,
    period_to: str,
    ticker: str = "LKOH",
    board: str = "TQBR",
    live: bool = False,
    mode: str | None = None,
    candles_df: pd.DataFrame | None = None,
    cache_root: Path | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    requested_mode = normalize_market_mode(mode, live)
    source = "fixture_candles"
    from_date, to_date = period_date_range(period_from, period_to)
    if candles_df is not None:
        df = normalize_candles(candles_df)
        source = "mocked"
        requested_mode = "mocked" if mode is None else requested_mode
    elif requested_mode == "live":
        try:
            df = normalize_candles(MoexClient().get_security_candles(ticker, board, from_date, to_date))
            source = "MOEX ISS"
            save_cached_candles(df, ticker, board, from_date, to_date, cache_root=cache_root)
        except MoexClientError as exc:
            warnings.append(f"MOEX ISS candles unavailable: {exc}")
            df = pd.DataFrame()
            source = "MOEX ISS"
    elif requested_mode == "replay_cache":
        cache_path = market_cache_path(ticker, board, from_date, to_date, cache_root=cache_root)
        if cache_path.exists():
            df = normalize_candles(pd.read_csv(cache_path))
            source = "market_cache"
        else:
            warnings.append("Cached market data not found; run live market audit first.")
            df = pd.DataFrame()
            source = "market_cache"
    else:
        df = load_fixture_candles(ticker)
        warnings.append("Live MOEX ISS fetch was not requested; local fixture/mock candles used for offline audit.")

    report = build_market_audit_report(
        ticker,
        board,
        period_from,
        period_to,
        df,
        source,
        warnings,
        market_data_mode=requested_mode,
    )
    save_market_audit_report(report)
    return report


def run_market_technical_report(
    ticker: str,
    period_from: str,
    period_to: str,
    provider: str = "moex-iss",
    board: str = "TQBR",
    mode: str = "replay-cache",
    candles_df: pd.DataFrame | None = None,
    cache_root: Path | None = None,
) -> dict[str, Any]:
    if provider.replace("_", "-") != "moex-iss":
        raise ValueError(f"Unsupported market provider: {provider}")
    report = run_market_audit(
        period_from,
        period_to,
        ticker=ticker,
        board=board,
        mode=mode,
        candles_df=candles_df,
        cache_root=cache_root,
    )
    report["provider"] = "moex_iss"
    report["report_type"] = "market_technical_report"
    report["volume_unit"] = "unknown_or_lots_or_securities"
    report["turnover_unit"] = "RUB"
    report["provider_columns"] = provider_columns_from_report(report, cache_root=cache_root)
    report["provider_endpoint"] = provider_endpoint(ticker, board)
    report["latest_summary"] = latest_market_summary(report)
    report["warnings"] = sorted(set(report.get("warnings", []) + date_to_warnings(report)))
    report["status"] = determine_market_technical_status(report)
    return report


def build_market_audit_report(
    ticker: str,
    board: str,
    period_from: str,
    period_to: str,
    df: pd.DataFrame,
    source: str,
    warnings: list[str] | None = None,
    market_data_mode: str | None = None,
) -> dict[str, Any]:
    warnings = list(warnings or [])
    df = normalize_candles(df)
    requested_from, requested_to = period_date_range(period_from, period_to)
    alignment = market_period_alignment(df, requested_from, requested_to)
    effective_source = adjusted_market_source(source, market_data_mode, alignment["market_period_aligned"])
    technical = technical_indicator_audit(df, applicable_to_requested_period=alignment["market_period_aligned"])
    liquidity = liquidity_metrics_audit(df)
    valuation = valuation_inputs_audit()
    if source != "MOEX ISS":
        warnings.append("Market audit did not use live MOEX ISS data.")
    if not df.empty and source == "fixture_candles":
        warnings.append("Fixture/mock candles are not proof of live LKOH MOEX market-data availability.")
    if not alignment["market_period_aligned"]:
        warnings.extend(alignment_warnings(source, alignment))
    summary = {
        "candles_count": int(len(df)),
        "first_trade_date": None if df.empty else str(df["date"].min()),
        "last_trade_date": None if df.empty else str(df["date"].max()),
        "trading_days_count": int(len(df)),
        "missing_trading_days_count": 0,
        "technical_indicators_valid_count": sum(1 for item in technical if item["status"] == "valid"),
        "technical_indicators_missing_count": sum(1 for item in technical if item["status"] == "missing"),
        "technical_indicators_not_applicable_count": sum(
            1 for item in technical if item["status"] == "not_applicable_to_requested_period"
        ),
        "liquidity_metrics_valid_count": sum(1 for item in liquidity if item["status"] == "valid"),
        "liquidity_metrics_missing_count": sum(1 for item in liquidity if item["status"] == "missing"),
        "valuation_inputs_available": all(item["status"] == "available" for item in valuation),
    }
    status = determine_market_status(summary, effective_source, alignment["market_period_aligned"])
    return {
        "company": ticker.upper(),
        "ticker": ticker,
        "board": board,
        "period_from": period_from,
        "period_to": period_to,
        "market_data_mode": market_data_mode or ("live" if source == "MOEX ISS" else "fixture"),
        "market_data_source": effective_source,
        "provider": "moex_iss" if source == "MOEX ISS" else None,
        "provider_endpoint": df.attrs.get("provider_endpoint"),
        "provider_columns": list(df.attrs.get("provider_columns") or df.columns),
        "volume_unit": "unknown_or_lots_or_securities",
        "turnover_unit": "RUB",
        "requested_date_range": {"from": requested_from, "to": requested_to},
        "actual_candle_date_range": alignment["actual_candle_date_range"],
        "market_period_aligned": alignment["market_period_aligned"],
        "coverage": alignment["coverage"],
        "status": status,
        "summary": summary,
        "technical_indicators": technical,
        "liquidity_metrics": liquidity,
        "valuation_inputs": valuation,
        "latest_summary": latest_market_summary_from_parts(df, technical),
        "warnings": sorted(set(warnings + liquidity_warnings(liquidity) + valuation_warnings(valuation))),
        "next_actions": next_actions(status, effective_source, valuation, alignment["market_period_aligned"]),
    }


def technical_indicator_audit(
    df: pd.DataFrame, applicable_to_requested_period: bool = True
) -> list[dict[str, Any]]:
    indicators = calculate_technical_indicators(df).get("indicators", {})
    available = int(len(df))
    rows = []
    for code, required in TECHNICAL_REQUIREMENTS.items():
        payload = indicators.get(code, {"value": None, "warnings": ["indicator not calculated"]})
        value = payload.get("value")
        status = "valid" if value is not None and available >= required else "missing"
        warnings = list(payload.get("warnings") or [])
        if code == "max_drawdown" and value is not None and value > 0:
            status = "questionable"
            warnings.append("max_drawdown should not be positive")
        if code == "rsi_14" and value is not None and not 0 <= value <= 100:
            status = "questionable"
            warnings.append("RSI should be in range 0..100")
        if status == "missing" and not warnings:
            warnings.append(f"{code} requires at least {required} observations")
        calculation_valid = status == "valid"
        if calculation_valid and not applicable_to_requested_period:
            status = "not_applicable_to_requested_period"
            warnings.append("Indicator is calculated on available candles outside the requested analysis period.")
        rows.append(
            {
                "indicator_code": code,
                "status": status,
                "value": value,
                "required_observations": required,
                "available_observations": available,
                "calculation_valid_on_available_candles": calculation_valid,
                "applicable_to_requested_period": applicable_to_requested_period,
                "warnings": warnings,
            }
        )
    return rows


def liquidity_metrics_audit(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return [
            missing_liquidity("average_daily_volume", "no_candles"),
            missing_liquidity("average_daily_turnover", "no_candles"),
            missing_liquidity("median_daily_turnover", "no_candles"),
            missing_liquidity("trading_days_count", "no_candles"),
            missing_liquidity("zero_volume_days_count", "no_candles"),
            bid_ask_missing(),
        ]
    volume = pd.to_numeric(df.get("volume"), errors="coerce")
    value = pd.to_numeric(df.get("value"), errors="coerce")
    rows = [
        liquidity_value("average_daily_volume", volume.mean()),
        liquidity_value("average_daily_turnover", value.mean()),
        liquidity_value("median_daily_turnover", value.median()),
        liquidity_value("trading_days_count", len(df)),
        liquidity_value("zero_volume_days_count", int((volume.fillna(0) == 0).sum())),
        bid_ask_missing(),
    ]
    return rows


def valuation_inputs_audit() -> list[dict[str, Any]]:
    return [
        {
            "input_code": "market_cap",
            "status": "missing",
            "missing_reason": "shares_outstanding_or_market_cap_unavailable",
        },
        {
            "input_code": "enterprise_value",
            "status": "missing",
            "missing_reason": "market_cap_and_net_debt_required",
        },
        {
            "input_code": "dividends",
            "status": "missing",
            "missing_reason": "dividend_data_unavailable",
        },
    ]


def market_analysis_from_audit(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_data_mode": report.get("market_data_mode"),
        "market_data_source": report["market_data_source"],
        "requested_date_range": report.get("requested_date_range"),
        "actual_candle_date_range": report.get("actual_candle_date_range"),
        "market_period_aligned": report.get("market_period_aligned"),
        "coverage": report.get("coverage"),
        "candles_summary": {
            "source_type": report["market_data_source"],
            "rows": report["summary"]["candles_count"],
            "from": report["summary"]["first_trade_date"],
            "to": report["summary"]["last_trade_date"],
        },
        "technical_indicators": {item["indicator_code"]: item for item in report["technical_indicators"]},
        "liquidity_metrics": {item["metric_code"]: item for item in report["liquidity_metrics"]},
        "valuation_inputs": {item["input_code"]: item for item in report["valuation_inputs"]},
        "latest_summary": report.get("latest_summary"),
        "warnings": report["warnings"],
    }


def normalize_candles(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "value"])
    out = df.copy()
    out.attrs.update(getattr(df, "attrs", {}) or {})
    if "begin" in out.columns and "date" not in out.columns:
        out = out.rename(columns={"begin": "date"})
    if "tradedate" in out.columns and "date" not in out.columns:
        out = out.rename(columns={"tradedate": "date"})
    for col in ["open", "high", "low", "close", "volume", "value"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"]).dt.date
    return out.sort_values("date").reset_index(drop=True)


def load_fixture_candles(ticker: str) -> pd.DataFrame:
    path = get_settings().root_dir / "data" / "fixtures" / f"{ticker.casefold()}_candles.csv"
    if not path.exists():
        return pd.DataFrame()
    return normalize_candles(pd.read_csv(path))


def market_cache_path(
    ticker: str,
    board: str,
    from_date: str,
    to_date: str,
    cache_root: Path | None = None,
) -> Path:
    root = cache_root or (get_settings().root_dir / "data" / "market_cache")
    return root / ticker.upper() / board.upper() / f"{from_date}_{to_date}_candles.csv"


def save_cached_candles(
    df: pd.DataFrame,
    ticker: str,
    board: str,
    from_date: str,
    to_date: str,
    cache_root: Path | None = None,
) -> Path:
    path = market_cache_path(ticker, board, from_date, to_date, cache_root=cache_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalize_candles(df).to_csv(path, index=False)
    return path


def save_market_audit_report(report: dict[str, Any]) -> Path:
    root = get_settings().root_dir / "data" / "validation" / report["ticker"].upper()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{report['period_from']}_{report['period_to']}_market_audit.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def save_market_technical_report(report: dict[str, Any]) -> Path:
    root = get_settings().root_dir / "data" / "validation" / report["ticker"].upper()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{report['period_from']}_{report['period_to']}_market_technical_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def period_date_range(period_from: str, period_to: str) -> tuple[str, str]:
    start_year, start_quarter = period_key(period_from)
    end_year, end_quarter = period_key(period_to)
    start_month = (start_quarter - 1) * 3 + 1
    end_month = end_quarter * 3
    end_day = 31 if end_month in {3, 12} else 30
    return str(date(start_year, start_month, 1)), str(date(end_year, end_month, end_day))


def normalize_market_mode(mode: str | None, live: bool) -> str:
    if live:
        return "live"
    normalized = (mode or "fixture").replace("-", "_")
    if normalized not in {"fixture", "live", "replay_cache", "mocked"}:
        raise ValueError(f"Unsupported market data mode: {mode}")
    return normalized


def market_period_alignment(df: pd.DataFrame, requested_from: str, requested_to: str) -> dict[str, Any]:
    request_start = date.fromisoformat(requested_from)
    request_end = date.fromisoformat(requested_to)
    requested_days = max((request_end - request_start).days + 1, 1)
    if df.empty:
        return {
            "actual_candle_date_range": {"from": None, "to": None},
            "market_period_aligned": False,
            "coverage": {
                "requested_trading_days_estimate": None,
                "covered_calendar_days": 0,
                "coverage_ratio": 0.0,
            },
        }
    actual_start = df["date"].min()
    actual_end = df["date"].max()
    if not isinstance(actual_start, date):
        actual_start = pd.to_datetime(actual_start).date()
    if not isinstance(actual_end, date):
        actual_end = pd.to_datetime(actual_end).date()
    overlap_start = max(request_start, actual_start)
    overlap_end = min(request_end, actual_end)
    covered_days = max((overlap_end - overlap_start).days + 1, 0) if overlap_start <= overlap_end else 0
    candles_within_requested = bool(((df["date"] >= request_start) & (df["date"] <= request_end)).any())
    aligned = candles_within_requested and covered_days > 0
    return {
        "actual_candle_date_range": {"from": str(actual_start), "to": str(actual_end)},
        "market_period_aligned": aligned,
        "coverage": {
            "requested_trading_days_estimate": None,
            "covered_calendar_days": covered_days,
            "coverage_ratio": round(covered_days / requested_days, 6),
        },
    }


def adjusted_market_source(source: str, mode: str | None, aligned: bool) -> str:
    if aligned:
        return source
    normalized_mode = (mode or "").replace("-", "_")
    if source == "fixture_candles":
        return "fixture_candles_unaligned"
    if source == "mocked":
        return "mocked_unaligned"
    if normalized_mode == "replay_cache" or source == "market_cache":
        return "market_cache_unaligned"
    return source


def alignment_warnings(source: str, alignment: dict[str, Any]) -> list[str]:
    actual_range = alignment.get("actual_candle_date_range") or {}
    warnings = []
    if actual_range.get("from") is None:
        warnings.append("No market candles are available for the requested analysis period.")
    elif alignment["coverage"]["covered_calendar_days"] == 0:
        warnings.append("Market candle date range does not overlap requested analysis period.")
    else:
        warnings.append("Market candle date range only partially overlaps requested analysis period.")
    if source == "fixture_candles":
        warnings.append(
            "Market indicators are based on fixture data outside the requested analysis period and must not be "
            "interpreted as 2021 LKOH market analysis."
        )
    return warnings


def determine_market_status(summary: dict[str, Any], source: str, market_period_aligned: bool) -> str:
    if summary["candles_count"] == 0:
        return "FAIL"
    if (
        source != "MOEX ISS"
        or not market_period_aligned
        or summary["technical_indicators_missing_count"]
        or summary["valuation_inputs_available"] is False
    ):
        return "PARTIAL"
    return "PASS"


def determine_market_technical_status(report: dict[str, Any]) -> str:
    summary = report["summary"]
    if summary["candles_count"] == 0:
        return "FAIL"
    if report.get("market_period_aligned") is not True:
        return "PARTIAL"
    core_liquidity = {"average_daily_volume", "average_daily_turnover", "median_daily_turnover", "trading_days_count"}
    liquidity_by_code = {item["metric_code"]: item for item in report.get("liquidity_metrics", [])}
    if any(liquidity_by_code.get(code, {}).get("status") != "valid" for code in core_liquidity):
        return "PARTIAL"
    core_technical = {"ma_20", "ma_50", "rsi_14", "support_level", "resistance_level"}
    technical_by_code = {item["indicator_code"]: item for item in report.get("technical_indicators", [])}
    if any(technical_by_code.get(code, {}).get("status") != "valid" for code in core_technical):
        return "PARTIAL"
    return "PASS"


def liquidity_value(code: str, value: Any) -> dict[str, Any]:
    return {
        "metric_code": code,
        "status": "valid",
        "value": None if pd.isna(value) else float(value),
        "source_field": "provider_volume" if "volume" in code else "provider_turnover",
        "warnings": [],
    }


def missing_liquidity(code: str, reason: str) -> dict[str, Any]:
    return {"metric_code": code, "status": "missing", "value": None, "missing_reason": reason, "warnings": [reason]}


def bid_ask_missing() -> dict[str, Any]:
    return {
        "metric_code": "bid_ask_spread",
        "status": "missing",
        "value": None,
        "reason": "requires_orderbook_or_bid_ask_data",
        "missing_reason": "requires_orderbook_or_bid_ask_data",
        "warning": "Daily candles do not contain bid/ask spread.",
        "warnings": ["Daily candles do not contain bid/ask spread."],
    }


def latest_market_summary(report: dict[str, Any]) -> dict[str, Any]:
    return report.get("latest_summary") or {}


def latest_market_summary_from_parts(df: pd.DataFrame, technical: list[dict[str, Any]]) -> dict[str, Any]:
    indicators = {item["indicator_code"]: item for item in technical}
    latest = {} if df.empty else df.iloc[-1].to_dict()
    return {
        "date": None if not latest else str(latest.get("date")),
        "close": None if not latest or pd.isna(latest.get("close")) else float(latest.get("close")),
        "ma_20": indicators.get("ma_20", {}).get("value"),
        "ma_50": indicators.get("ma_50", {}).get("value"),
        "rsi_14": indicators.get("rsi_14", {}).get("value"),
        "support_level": indicators.get("support_level", {}).get("value"),
        "resistance_level": indicators.get("resistance_level", {}).get("value"),
    }


def date_to_warnings(report: dict[str, Any]) -> list[str]:
    requested_to = (report.get("requested_date_range") or {}).get("to")
    actual_to = (report.get("actual_candle_date_range") or {}).get("to")
    if requested_to and actual_to and actual_to < requested_to:
        return ["date_to_not_present_if_non_trading_or_provider_omitted"]
    return []


def provider_endpoint(ticker: str, board: str) -> str:
    base_url = get_settings().moex_base_url.rstrip("/")
    return f"{base_url}/engines/stock/markets/shares/boards/{board}/securities/{ticker}/candles.json"


def provider_columns_from_report(report: dict[str, Any], cache_root: Path | None = None) -> list[str]:
    existing = report.get("provider_columns") or []
    if existing:
        return existing
    requested = report.get("requested_date_range") or {}
    cache_path = market_cache_path(
        report["ticker"],
        report["board"],
        requested.get("from", ""),
        requested.get("to", ""),
        cache_root=cache_root,
    )
    if cache_path.exists():
        return list(pd.read_csv(cache_path, nrows=0).columns)
    return ["date", "open", "high", "low", "close", "volume", "value"]


def liquidity_warnings(rows: list[dict[str, Any]]) -> list[str]:
    return [warning for row in rows for warning in row.get("warnings", [])]


def valuation_warnings(rows: list[dict[str, Any]]) -> list[str]:
    return [f"{row['input_code']} missing: {row['missing_reason']}" for row in rows if row["status"] == "missing"]


def next_actions(status: str, source: str, valuation: list[dict[str, Any]], market_period_aligned: bool) -> list[str]:
    actions = []
    if source != "MOEX ISS":
        actions.append("Run optional live MOEX ISS audit to prove live LKOH market candle availability.")
    if not market_period_aligned:
        actions.append("Use MOEX candles that overlap the requested analysis period before interpreting indicators.")
    if any(item["status"] == "missing" for item in valuation):
        actions.append("Add market cap/shares, EV components, and dividend data before calculating valuation ratios.")
    if status != "PASS":
        actions.append("Keep market/valuation analytics partial until live data and valuation inputs are validated.")
    return actions
