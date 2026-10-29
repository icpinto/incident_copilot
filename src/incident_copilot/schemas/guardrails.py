"""Content-free diagnostics for optional model policy checks."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

GuardrailMode = Literal["off", "observe", "enforce"]


class GuardrailEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    stage: Literal["input", "output"]
    status: Literal["pending", "passed", "flagged", "error", "skipped"]
    code: str
    model_call: int = Field(ge=0)
    duration_ms: float = Field(default=0, ge=0)


class GuardrailSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: GuardrailMode
    policy_version: str
    checks: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    duration_ms: float = Field(default=0, ge=0)
    events: list[GuardrailEvent] = Field(default_factory=list)
