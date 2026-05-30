from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Company, PeerGroup


class PeerRegistry:
    def __init__(self, db: Session):
        self.db = db

    def peers_for(self, company_id: int) -> list[Company]:
        rows = self.db.scalars(
            select(PeerGroup).where(PeerGroup.company_id == company_id).order_by(PeerGroup.peer_rank)
        ).all()
        return [row.peer_company for row in rows]

