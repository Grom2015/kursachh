from pydantic import BaseModel, ConfigDict


class CompanyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticker: str
    short_name: str
    full_name: str
    sector: str | None
    board: str
    aliases: list[str]


class CompanySearchResponse(BaseModel):
    items: list[CompanyOut]

