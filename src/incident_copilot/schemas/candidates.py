from typing import List, Optional
from pydantic import BaseModel


class CandidateLocation(BaseModel):
    file_path: str
    symbol_name: Optional[str] = None
    service_name: Optional[str] = None
    score: float
    reasons: List[str] = []
    snippet: Optional[str] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None


class CandidateSet(BaseModel):
    items: List[CandidateLocation]
