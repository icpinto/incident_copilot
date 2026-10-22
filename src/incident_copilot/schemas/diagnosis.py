from typing import List, Optional
from pydantic import BaseModel, Field

from .candidates import CandidateLocation


class Hypothesis(BaseModel):
    rank: int
    title: str
    explanation: str
    evidence: List[str]
    confidence: float = Field(ge=0, le=1)
    next_checks: List[str]


class DiagnosisReport(BaseModel):
    incident_id: str
    likely_service: Optional[str]
    likely_error_class: Optional[str]
    candidates: List[CandidateLocation]
    hypotheses: List[Hypothesis]
    summary: str
    engine: str = "deterministic+llm"
