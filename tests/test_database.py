from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.init_db import seed_companies, seed_peers
from app.db.models import Company, MarketCandle, PeerGroup


def test_seed_is_idempotent(db_session):
    seed_companies(db_session)
    seed_peers(db_session)
    company_count = db_session.scalar(select(func.count()).select_from(Company))
    peer_count = db_session.scalar(select(func.count()).select_from(PeerGroup))
    assert company_count == 6
    assert peer_count == 5


def test_sqlite_fallback_schema_creation():
    db_path = Path(__file__).resolve().parent / "fallback_runtime.db"
    if db_path.exists():
        db_path.unlink()
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        session.add(Company(ticker="TEST", board="TQBR", short_name="Test", full_name="Test"))
        session.commit()
        assert session.scalar(select(Company).where(Company.ticker == "TEST")) is not None
    engine.dispose()
    if db_path.exists():
        db_path.unlink()


def test_company_ticker_board_unique(db_session):
    db_session.add(Company(ticker="LKOH", board="TQBR", short_name="Duplicate", full_name="Duplicate"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_market_candle_unique(db_session):
    kwargs = {
        "company_id": 1,
        "ticker": "LKOH",
        "board": "TQBR",
        "trade_date": date(2025, 1, 1),
        "close": 1,
        "source": "fixture",
    }
    db_session.add(MarketCandle(**kwargs))
    db_session.commit()
    db_session.add(MarketCandle(**kwargs))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
