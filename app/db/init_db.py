from pathlib import Path

import yaml
from sqlalchemy import select

from app.core.config import get_settings
from app.db.base import Base
from app.db.models import Company, PeerGroup
from app.db.schema_guard import ensure_sqlite_compat_schema
from app.db.session import SessionLocal, engine


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def seed_companies(db) -> None:
    root = get_settings().root_dir
    data = load_yaml(root / "data" / "seed" / "companies.yml")
    for item in data.get("companies", []):
        company = db.scalar(select(Company).where(Company.ticker == item["ticker"]))
        attrs = {
            "ticker": item["ticker"],
            "isin": item.get("isin"),
            "board": item.get("board") or "TQBR",
            "short_name": item["short_name"],
            "full_name": item["full_name"],
            "inn": item.get("inn"),
            "sector": item.get("sector"),
            "subsector": item.get("subsector"),
            "aliases_json": item.get("aliases") or [],
            "ir_url": item.get("ir_url"),
            "disclosure_id": item.get("disclosure_id"),
            "identity_status": "resolved_registry",
            "verification_scope": "registry",
            "is_active": True,
        }
        if company:
            for key, value in attrs.items():
                setattr(company, key, value)
        else:
            db.add(Company(**attrs))
    db.commit()


def seed_peers(db) -> None:
    root = get_settings().root_dir
    data = load_yaml(root / "data" / "seed" / "peer_groups.yml")
    for ticker, peers in data.get("peer_groups", {}).items():
        company = db.scalar(select(Company).where(Company.ticker == ticker))
        if not company:
            continue
        for item in peers:
            peer = db.scalar(select(Company).where(Company.ticker == item["ticker"]))
            if not peer:
                continue
            exists = db.scalar(
                select(PeerGroup).where(
                    PeerGroup.company_id == company.id,
                    PeerGroup.peer_company_id == peer.id,
                )
            )
            if exists:
                exists.peer_rank = item.get("peer_rank")
                exists.inclusion_reason = item.get("inclusion_reason")
            else:
                db.add(
                    PeerGroup(
                        company_id=company.id,
                        peer_company_id=peer.id,
                        sector=company.sector,
                        subsector=company.subsector,
                        peer_rank=item.get("peer_rank"),
                        inclusion_reason=item.get("inclusion_reason"),
                    )
                )
    db.commit()


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_sqlite_compat_schema(engine)
    with SessionLocal() as db:
        seed_companies(db)
        seed_peers(db)


if __name__ == "__main__":
    init_db()
