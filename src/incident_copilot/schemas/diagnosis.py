from pydantic import BaseModel, ConfigDict, Field

from incident_copilot.schemas.validation import AnalysisValidationError

from .candidates import CandidateLocation
from .code_graph import GraphContext
from .investigation import EvidenceItem, InvestigationEvent, InvestigationSummary


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1, le=3)
    title: str
    explanation: str
    evidence: list[str]
    evidence_refs: list[str] = Field(default_factory=list)
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
    graph_context: GraphContext | None = None
    retrieved_evidence: list[EvidenceItem] = Field(default_factory=list)
    investigation: InvestigationSummary | None = None
    investigation_events: list[InvestigationEvent] = Field(default_factory=list)
    validation_errors: list[AnalysisValidationError] = Field(default_factory=list)
