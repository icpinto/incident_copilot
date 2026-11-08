from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from incident_copilot.schemas.guardrails import GuardrailSummary
from incident_copilot.schemas.validation import AnalysisValidationError

from .candidates import CandidateLocation
from .code_graph import GraphContext
from .deployment import DeploymentContext, SourceResolution
from .investigation import EvidenceItem, InvestigationEvent, InvestigationSummary
from .logs import IncidentContext
from .reasoning import ReasoningSummary
from .recommendations import ActionEvent, HypothesisActionPlan, RunbookSnapshot
from .runtime import FailureLayer


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1, le=3)
    id: str | None = None
    action_plan: HypothesisActionPlan | None = None
    failure_layer: FailureLayer = "unknown"
    title: str
    explanation: str
    evidence: list[str]
    evidence_refs: list[str] = Field(default_factory=list)
    contradicting_evidence_refs: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    confidence_status: Literal["unavailable", "uncalibrated", "calibrated"] = "unavailable"
    model_score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    next_checks: list[str]

    @model_validator(mode="before")
    @classmethod
    def historical_confidence(cls, value):
        if isinstance(value, dict) and "confidence_status" not in value:
            value = dict(value)
            value["confidence_status"] = "uncalibrated" if value.get("confidence") is not None else "unavailable"
        return value

    @model_validator(mode="after")
    def coherent_confidence(self):
        if (self.confidence_status == "unavailable") != (self.confidence is None):
            raise ValueError("Confidence must be null exactly when its status is unavailable.")
        return self


class DiagnosisReport(BaseModel):
    incident_id: str
    reasoning: ReasoningSummary | None = None
    action_plan_version: Literal[1] | None = None
    runbooks: list[RunbookSnapshot] = Field(default_factory=list)
    recommendation_limitations: list[str] = Field(default_factory=list)
    follow_up: list[ActionEvent] = Field(default_factory=list)
    repository_source: dict[str, str] | None = None
    deployment_context: DeploymentContext | None = None
    source_resolution: SourceResolution = Field(default_factory=SourceResolution)
    likely_service: str | None
    likely_error_class: str | None
    candidates: list[CandidateLocation]
    hypotheses: list[Hypothesis]
    summary: str
    diagnosis_status: Literal["provisional", "insufficient_evidence", "unknown"] = "unknown"
    diagnosis_reason: str | None = None
    next_checks: list[str] = Field(default_factory=list)
    incident_context: IncidentContext | None = None
    engine: str = "deterministic+llm"
    fallback_reason: str | None = None
    graph_context: GraphContext | None = None
    retrieved_evidence: list[EvidenceItem] = Field(default_factory=list)
    runtime_collection: list[dict] = Field(default_factory=list)
    investigation: InvestigationSummary | None = None
    investigation_events: list[InvestigationEvent] = Field(default_factory=list)
    validation_errors: list[AnalysisValidationError] = Field(default_factory=list)
    guardrails: GuardrailSummary | None = None
