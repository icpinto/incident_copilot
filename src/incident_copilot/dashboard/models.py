from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from incident_copilot.schemas.candidates import CandidateLocation
from incident_copilot.schemas.code_graph import GraphContext, GraphPreparation
from incident_copilot.schemas.diagnosis import DiagnosisReport
from incident_copilot.schemas.index import RepoIndexSummary
from incident_copilot.schemas.investigation import (
    EvidenceItem,
    InvestigationEvent,
    InvestigationSummary,
)
from incident_copilot.schemas.logs import ParsedLog
from incident_copilot.schemas.validation import AnalysisValidationError


class RepositoryInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    repo_root: str = Field(min_length=1, max_length=4096)
    compose_path: str | None = None


class RepositoryValidation(BaseModel):
    valid: bool
    message: str
    services: list[dict[str, Any]] = Field(default_factory=list)


class RepositoryProfile(RepositoryInput):
    id: str
    archived: bool = False
    created_at: str
    validation: RepositoryValidation | None = None
    index_summary: dict[str, Any] | None = None


class IncidentSubmission(BaseModel):
    repository_id: str
    title: str = Field(default="", max_length=200)
    log_text: str = Field(min_length=1, max_length=2_000_000)
    user_hint_service: str | None = Field(default=None, max_length=200)


class StageEvent(BaseModel):
    stage: str
    started_at: str
    completed_at: str | None = None
    attempt: int = 1


class IncidentEvidence(BaseModel):
    raw_log: str = ""
    parsed_log: ParsedLog | None = None
    candidates: list[CandidateLocation] = Field(default_factory=list)
    deep_analysis: dict[str, Any] = Field(default_factory=dict)
    index_summary: RepoIndexSummary | None = None
    graph_context: GraphContext | None = None
    graph_preparation: GraphPreparation | None = None
    retrieved_evidence: list[EvidenceItem] = Field(default_factory=list)
    investigation: InvestigationSummary | None = None
    investigation_events: list[InvestigationEvent] = Field(default_factory=list)
    validation_errors: list[AnalysisValidationError] = Field(default_factory=list)


class IncidentRecord(BaseModel):
    id: str
    title: str
    repository_id: str
    repository_name: str
    status: Literal["queued", "running", "completed", "failed", "canceled"]
    created_at: str
    updated_at: str
    completed_at: str | None = None
    config_revision: str
    stage: str = "queued"
    timeline: list[StageEvent] = Field(default_factory=list)
    report: DiagnosisReport | None = None
    evidence: IncidentEvidence = Field(default_factory=IncidentEvidence)
    error: str | None = None
    connection_error: str | None = None


class IncidentPage(BaseModel):
    items: list[IncidentRecord]
    total: int
    page: int
    page_size: int


class SettingsUpdate(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    reset: list[str] = Field(default_factory=list)


class SettingsView(BaseModel):
    saved: dict[str, Any]
    active: dict[str, Any]
    overridden: list[str]
    restart_required: list[str]
    infrastructure_fields: list[str]
    control_db_path: str
    secret_configured: bool
    graph_secret_configured: bool = False


class ConnectionCheck(BaseModel):
    target: Literal["temporal", "llm", "storage"]


class ConnectionResult(BaseModel):
    ok: bool
    message: str


class Overview(BaseModel):
    total: int
    completed: int
    failed: int
    active: int
    average_duration_seconds: float
    activity: list[dict[str, Any]]
    services: list[dict[str, Any]]
    recent: list[IncidentRecord]
