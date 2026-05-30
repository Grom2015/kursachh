from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.company import CompanyOut, CompanySearchResponse
from app.services.company_registry import CompanyRegistry

router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("/search", response_model=CompanySearchResponse)
def search_companies(q: str, db: Session = Depends(get_db)) -> CompanySearchResponse:
    items = [
        CompanyOut(
            id=company.id,
            ticker=company.ticker,
            short_name=company.short_name,
            full_name=company.full_name,
            sector=company.sector,
            board=company.board,
            aliases=company.aliases_json or [],
        )
        for company in CompanyRegistry(db).search(q)
    ]
    return CompanySearchResponse(items=items)

