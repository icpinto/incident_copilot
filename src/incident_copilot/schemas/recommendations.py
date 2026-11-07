"""Version-one manual recommendations and responder-reported outcomes."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Text = Annotated[str, Field(min_length=1, max_length=3000)]


class RecommendationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SupportingClaim(RecommendationRecord):
    claim: Text
    evidence_refs: list[str] = Field(min_length=1, max_length=20)


class NextCheck(RecommendationRecord):
    id: str = ""
    instruction: Text
    context: Text
    command: Text | None = None
    placeholders: list[Text] = Field(default_factory=list, max_length=10)
    evidence_to_collect: Text
    supports_hypothesis: Text
    weakens_hypothesis: Text


class RunbookReference(RecommendationRecord):
    runbook_id: str
    section: str
    reason: Text


class RecoveryVerification(RecommendationRecord):
    measure: Text
    success_condition: Text
    observation_period: Text


class MitigationProposal(RecommendationRecord):
    id: str = ""
    title: Text
    intended_effect: Text
    prerequisites: list[Text] = Field(min_length=1, max_length=10)
    missing_prerequisites: list[Text] = Field(default_factory=list, max_length=10)
    risks: list[Text] = Field(min_length=1, max_length=10)
    affected_services: list[Text] = Field(min_length=1, max_length=10)
    steps: list[Text] = Field(min_length=1, max_length=10)
    rollback: list[Text] = Field(min_length=1, max_length=10)
    verification: RecoveryVerification
    runbook_ids: list[str] = Field(default_factory=list, max_length=3)


class HypothesisActionPlan(RecommendationRecord):
    version: Literal[1] = 1
    supporting_evidence: list[SupportingClaim] = Field(min_length=1, max_length=10)
    missing_evidence: list[Text] = Field(max_length=10)
    checks: list[NextCheck] = Field(min_length=1, max_length=3)
    runbooks: list[RunbookReference] = Field(max_length=3)
    mitigations: list[MitigationProposal] = Field(max_length=2)


class RunbookSection(RecommendationRecord):
    heading: str
    line_start: int
    line_end: int
    content: str


class RunbookSnapshot(RecommendationRecord):
    id: str
    title: str
    file_path: str
    revision: str | None = None
    content_hash: str
    services: list[str]
    error_classes: list[str]
    failure_layers: list[str]
    sections: list[RunbookSection]


CheckStatus = Literal["supports", "weakens", "inconclusive", "blocked", "skipped"]
MitigationStatus = Literal["in_progress", "applied", "verified", "failed", "rolled_back", "skipped"]


class ActionEventWrite(RecommendationRecord):
    client_event_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    action_id: str = Field(min_length=1, max_length=256)
    status: CheckStatus | MitigationStatus
    observations: str = Field(default="", max_length=8000)
    responder: str | None = Field(default=None, max_length=120)
    useful: bool | None = None

    @field_validator("observations", "responder")
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value


class ActionEvent(ActionEventWrite):
    incident_id: str
    action_kind: Literal["check", "mitigation"]
    recorded_at: str
