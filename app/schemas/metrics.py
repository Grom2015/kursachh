from pydantic import BaseModel


class MetricOut(BaseModel):
    metric_code: str
    period: str
    value: float | None
    display_value: str | None
    quality_flag: str
    warnings: list[str]

