from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.core.build_info import current_build_info
from app.core.config import get_settings

router = APIRouter(tags=["ui"])


@router.get("/app", response_class=HTMLResponse)
def control_center_app() -> HTMLResponse:
    path = get_settings().root_dir / "app" / "web" / "index.html"
    build = current_build_info()
    html = path.read_text(encoding="utf-8")
    html = html.replace("__APP_VERSION__", build.app_version)
    html = html.replace("__BUILD_ID__", build.build_id)
    html = html.replace("/app/assets/control-center.css", f"/app/assets/control-center.css?v={build.build_id}")
    html = html.replace("/app/assets/control-center.js?v=10", f"/app/assets/control-center.js?v={build.build_id}")
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


def static_assets_dir() -> Path:
    return get_settings().root_dir / "app" / "web" / "assets"
