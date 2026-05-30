from pydantic import BaseModel


class IndicatorOut(BaseModel):
    value: float | None
    quality_flag: str
    warnings: list[str]

