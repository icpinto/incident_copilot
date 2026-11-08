"""Versioned, concise reasoning metadata; no private reasoning traces."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ReasoningMode = Literal["baseline", "structured", "structured_review"]
ReviewReason = Literal["competing_causes", "conflicting_evidence", "causal_gap"]


class ReasoningSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: ReasoningMode = "baseline"
    prompt_version: str = "baseline-v1"
    review_status: Literal[
        "disabled", "not_needed", "pending", "completed", "failed", "skipped"
    ] = "disabled"
    review_reasons: list[ReviewReason] = Field(default_factory=list)
    review_failure: str | None = None
    model_calls: int = Field(default=0, ge=0)
