from pydantic import BaseModel


class SourceDocumentOut(BaseModel):
    id: int
    period: str
    source_type: str
    status: str

