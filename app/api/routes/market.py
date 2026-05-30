from fastapi import APIRouter

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/health")
def market_health() -> dict[str, str]:
    return {"status": "ok"}

