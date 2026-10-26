
from pydantic import BaseModel


class CandidateLocation(BaseModel):
    file_path: str
    symbol_name: str | None = None
    service_name: str | None = None
    score: float
    reasons: list[str] = []
    snippet: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    group_id: str | None = None
    event_ids: list[str] = []


class CandidateSet(BaseModel):
    items: list[CandidateLocation]
