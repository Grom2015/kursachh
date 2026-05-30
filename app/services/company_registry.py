import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Company


def normalize_query(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").strip().casefold()
    return " ".join(value.split())


class CompanyRegistry:
    def __init__(self, db: Session):
        self.db = db

    def search(self, query: str, limit: int = 20) -> list[Company]:
        q = normalize_query(query)
        if not q:
            return []
        companies = self.db.scalars(select(Company).where(Company.is_active.is_(True))).all()
        scored: list[tuple[int, Company]] = []
        for company in companies:
            values = [
                company.ticker,
                company.short_name,
                company.full_name,
                *(company.aliases_json or []),
            ]
            normalized = [normalize_query(v) for v in values if v]
            exact = any(q == item for item in normalized)
            partial = any(q in item or item in q for item in normalized)
            if exact or partial:
                scored.append((0 if exact else 1, company))
        scored.sort(key=lambda item: (item[0], item[1].ticker))
        return [company for _, company in scored[:limit]]

    def resolve_one(self, query: str) -> Company | None:
        items = self.search(query, limit=5)
        return items[0] if len(items) == 1 else None

