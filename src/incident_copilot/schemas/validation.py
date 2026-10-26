"""Public diagnostics contain schema information, never rejected input values."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ValidationStage = Literal["hypothesis_generation", "investigation_assessment", "evidence_retrieval"]
ValidationKind = Literal["json", "schema", "semantic", "limit"]


class ValidationResult(BaseModel):
    commands_run: list[str] = Field(default_factory=list)
    passed: bool = False
    output: str = ""


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: list[str | int] = Field(default_factory=list)
    code: str
    message: str


class AnalysisValidationError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: ValidationStage
    kind: ValidationKind
    model_call: int | None = Field(default=None, ge=1)
    request_id: str | None = None
    issues: list[ValidationIssue] = Field(min_length=1, max_length=30)
    total_issues: int = Field(ge=1)
