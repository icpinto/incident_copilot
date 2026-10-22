from pydantic import BaseModel, ConfigDict, Field

from .candidates import CandidateLocation


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1, le=3)
    title: str
    explanation: str
    evidence: list[str]
    confidence: float = Field(ge=0, le=1)
    next_checks: list[str]


class DiagnosisReport(BaseModel):
    incident_id: str
    likely_service: str | None
    likely_error_class: str | None
    candidates: list[CandidateLocation]
    hypotheses: list[Hypothesis]
    summary: str
    engine: str = "deterministic+llm"
    fallback_reason: str | None = None
