from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    app_version: str | None = None
    build_id: str | None = None
    started_at: str | None = None
    process_id: int | None = None
