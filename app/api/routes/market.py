from datetime import datetime

import pandas as pd
from fastapi import APIRouter, HTTPException

from app.core.config import get_settings
from app.services.market.market_audit import (
    normalize_candles,
    period_date_range,
    run_market_technical_report,
)
from app.services.market.moex_client import MoexClient

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/health")
def market_health() -> dict[str, str]:
    return {"status": "ok"}


def _default_period_range() -> tuple[str, str]:
    """Default to ~2 years so long indicators like MA200 have enough candles."""
    year = datetime.now().year
    return f"{year - 1}Q1", f"{year}Q4"


@router.get("/technical")
def market_technical(
    ticker: str,
    period_from: str | None = None,
    period_to: str | None = None,
    board: str = "TQBR",
) -> dict:
    default_from, default_to = _default_period_range()
    period_from = (period_from or default_from).strip().upper()
    period_to = (period_to or default_to).strip().upper()
    ticker = ticker.strip().upper()
    board = (board or "TQBR").strip().upper()
    if not ticker:
        raise HTTPException(status_code=422, detail="ticker_required")
    cache_root = get_settings().root_dir / "data" / "market_cache"
    try:
        report = run_market_technical_report(
            ticker=ticker,
            period_from=period_from,
            period_to=period_to,
            board=board,
            mode="live",
            cache_root=cache_root,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        return {
            "ticker": ticker,
            "board": board,
            "period_from": period_from,
            "period_to": period_to,
            "status": "UNAVAILABLE",
            "summary": {"candles_count": 0},
            "technical_indicators": [],
            "liquidity_metrics": [],
            "warnings": [f"market_data_fetch_failed: {exc}"],
        }
    return report


@router.get("/candles")
def market_candles(
    ticker: str,
    period_from: str | None = None,
    period_to: str | None = None,
    board: str = "TQBR",
) -> dict:
    default_from, default_to = _default_period_range()
    period_from = (period_from or default_from).strip().upper()
    period_to = (period_to or default_to).strip().upper()
    ticker = ticker.strip().upper()
    board = (board or "TQBR").strip().upper()
    if not ticker:
        raise HTTPException(status_code=422, detail="ticker_required")
    from_date, to_date = period_date_range(period_from, period_to)
    try:
        df = normalize_candles(MoexClient().get_security_candles(ticker, board, from_date, to_date))
    except Exception as exc:
        return {
            "ticker": ticker,
            "board": board,
            "period_from": period_from,
            "period_to": period_to,
            "count": 0,
            "candles": [],
            "warnings": [f"candles_fetch_failed: {exc}"],
        }

    def _num(value: object) -> float | None:
        if value is None or pd.isna(value):
            return None
        return float(value)

    candles = [
        {
            "date": str(row.get("date")),
            "open": _num(row.get("open")),
            "high": _num(row.get("high")),
            "low": _num(row.get("low")),
            "close": _num(row.get("close")),
            "volume": _num(row.get("volume")),
        }
        for row in df.to_dict("records")
    ]
    return {
        "ticker": ticker,
        "board": board,
        "period_from": period_from,
        "period_to": period_to,
        "count": len(candles),
        "candles": candles,
        "warnings": [],
    }
