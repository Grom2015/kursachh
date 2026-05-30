from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.build_info import current_build_info
from app.schemas.common import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> JSONResponse:
    build = current_build_info()
    return JSONResponse(
        {
            "status": "ok",
            "app_version": build.app_version,
            "build_id": build.build_id,
            "started_at": build.started_at,
            "process_id": build.process_id,
        },
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )
