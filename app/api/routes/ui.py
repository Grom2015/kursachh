from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.build_info import current_build_info
from app.core.config import get_settings

router = APIRouter(tags=["ui"])


@router.get("/", include_in_schema=False)
def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/app")


@router.get("/app", response_class=HTMLResponse)
def control_center_app() -> HTMLResponse:
    path = get_settings().root_dir / "app" / "web" / "index.html"
    return _render_app_html(path)


@router.get("/analysis-app", response_class=HTMLResponse)
def pdf_analysis_app() -> HTMLResponse:
    path = get_settings().root_dir / "app" / "web" / "pdf-analysis.html"
    return _render_app_html(path)


def _render_app_html(path: Path) -> HTMLResponse:
    build = current_build_info()
    assets_dir = static_assets_dir()
    html = path.read_text(encoding="utf-8")
    html = html.replace(
        "/app/assets/control-center.css",
        f"/app/assets/control-center.css?v={_asset_version(assets_dir / 'control-center.css', build.build_id)}",
    )
    html = html.replace(
        "/app/assets/control-center.js?v=10",
        f"/app/assets/control-center.js?v={_asset_version(assets_dir / 'control-center.js', build.build_id)}",
    )
    html = html.replace(
        "/app/assets/app.css?v=__BUILD_ID__",
        f"/app/assets/app.css?v={_asset_version(assets_dir / 'app.css', build.build_id)}",
    )
    html = html.replace(
        "/app/assets/app.js?v=__BUILD_ID__",
        f"/app/assets/app.js?v={_asset_version(assets_dir / 'app.js', build.build_id)}",
    )
    html = html.replace("__APP_VERSION__", build.app_version)
    html = html.replace("__BUILD_ID__", build.build_id)
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


def static_assets_dir() -> Path:
    return get_settings().root_dir / "app" / "web" / "assets"


def _asset_version(path: Path, build_id: str) -> str:
    try:
        stamp = int(path.stat().st_mtime)
    except FileNotFoundError:
        stamp = 0
    return f"{build_id}-{stamp}"
