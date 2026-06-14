from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()


class Settings(BaseModel):
    app_env: str = "local"
    database_url: str = "sqlite:///./local.db"
    moex_base_url: str = "https://iss.moex.com/iss"
    cors_allowed_origins: list[str] = []
    data_mode_default: str = "fixture"
    report_download_dir: str = "data/raw"
    report_parsed_dir: str = "data/parsed"
    max_report_download_mb: int = 50
    report_tls_ca_bundle: str = ""
    report_source_allowed_domains: list[str] = [
        "lukoil.com",
        "www.lukoil.com",
        "lukoil.ru",
        "www.lukoil.ru",
        "tatneft.ru",
        "www.tatneft.ru",
        "old.tatneft.ru",
        "gazprom.com",
        "www.gazprom.com",
        "e-disclosure.ru",
        "www.e-disclosure.ru",
    ]
    enable_live_source_tests: bool = False
    lkoh_ir_reports_url: str | None = None
    anthropic_api_key: str | None = None
    llm_model: str = "claude-sonnet-4-20250514"
    llm_enabled: bool = False
    disclaimer: str = (
        "Материал носит информационно-аналитический характер и не является "
        "индивидуальной инвестиционной рекомендацией."
    )
    root_dir: Path = Path(__file__).resolve().parents[2]


@lru_cache
def get_settings() -> Settings:
    import os

    return Settings(
        app_env=os.getenv("APP_ENV", "local"),
        database_url=os.getenv("DATABASE_URL", "sqlite:///./local.db"),
        moex_base_url=os.getenv("MOEX_BASE_URL", "https://iss.moex.com/iss"),
        cors_allowed_origins=[
            origin.strip()
            for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
            if origin.strip()
        ],
        data_mode_default=os.getenv("DATA_MODE_DEFAULT", "fixture"),
        report_download_dir=os.getenv("REPORT_DOWNLOAD_DIR", "data/raw"),
        report_parsed_dir=os.getenv("REPORT_PARSED_DIR", "data/parsed"),
        max_report_download_mb=int(os.getenv("MAX_REPORT_DOWNLOAD_MB", "50")),
        report_tls_ca_bundle=os.getenv("REPORT_TLS_CA_BUNDLE", ""),
        report_source_allowed_domains=[
            domain.strip().lower()
            for domain in os.getenv(
                "REPORT_SOURCE_ALLOWED_DOMAINS",
                "lukoil.com,www.lukoil.com,lukoil.ru,www.lukoil.ru,tatneft.ru,www.tatneft.ru,old.tatneft.ru,gazprom.com,www.gazprom.com,e-disclosure.ru,www.e-disclosure.ru",
            ).split(",")
            if domain.strip()
        ],
        enable_live_source_tests=os.getenv("ENABLE_LIVE_SOURCE_TESTS", "false").casefold()
        == "true",
        lkoh_ir_reports_url=os.getenv("LKOH_IR_REPORTS_URL") or None,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        llm_model=os.getenv("LLM_MODEL", "claude-sonnet-4-20250514"),
        llm_enabled=os.getenv("LLM_ENABLED", "false").casefold() == "true",
    )
