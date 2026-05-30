from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Company, MarketCandle
from app.services.market.technical_indicators import calculate_technical_indicators


class MarketService:
    def __init__(self, db: Session):
        self.db = db

    def load_fixture_candles(self, company: Company) -> pd.DataFrame:
        path = Path(get_settings().root_dir) / "data" / "fixtures" / f"{company.ticker.casefold()}_candles.csv"
        if not path.exists():
            return pd.DataFrame()
        df = pd.read_csv(path)
        for _, row in df.iterrows():
            trade_date = pd.to_datetime(row["date"]).date()
            exists = self.db.scalar(
                select(MarketCandle).where(
                    MarketCandle.company_id == company.id,
                    MarketCandle.trade_date == trade_date,
                    MarketCandle.source == "fixture",
                )
            )
            if exists:
                continue
            self.db.add(
                MarketCandle(
                    company_id=company.id,
                    ticker=company.ticker,
                    board=company.board,
                    trade_date=trade_date,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    value=float(row["value"]),
                    source="fixture",
                )
            )
        self.db.flush()
        return df

    def analyze_fixture(self, company: Company) -> dict:
        df = self.load_fixture_candles(company)
        technical = calculate_technical_indicators(df)
        return {
            "candles_summary": {
                "source_type": "fixture" if not df.empty else "unavailable",
                "rows": int(len(df)),
                "from": None if df.empty else str(df["date"].iloc[0]),
                "to": None if df.empty else str(df["date"].iloc[-1]),
            },
            "technical_indicators": technical["indicators"],
            "warnings": technical["warnings"],
        }
