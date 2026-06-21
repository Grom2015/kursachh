from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routes import analysis, companies, health, market, pdf_analysis, pipeline, report_sources, reports, ui
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.base import Base
from app.db.schema_guard import ensure_sqlite_compat_schema
from app.db.session import engine


def create_app() -> FastAPI:
    configure_logging()
    Base.metadata.create_all(bind=engine)
    settings = get_settings()
    app = FastAPI(title="MOEX AI Financial Research Assistant API", version="0.1.0")
    ensure_sqlite_compat_schema(engine)
    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )
    app.include_router(health.router)
    app.include_router(companies.router)
    app.include_router(analysis.router)
    app.include_router(reports.router)
    app.include_router(report_sources.router)
    app.include_router(market.router)
    app.include_router(pipeline.router)
    app.include_router(pdf_analysis.router)
    app.include_router(ui.router)
    assets_dir = ui.static_assets_dir()
    assets_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/app/assets", StaticFiles(directory=assets_dir), name="app-assets")
    return app


app = create_app()
