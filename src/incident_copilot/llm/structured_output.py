from pydantic import BaseModel, ConfigDict, Field, field_validator

from incident_copilot.schemas.diagnosis import Hypothesis


class HypothesesResponse(BaseModel):
    """The shared generation and validation contract for both LLM adapters."""

    model_config = ConfigDict(extra="forbid")

    hypotheses: list[Hypothesis] = Field(min_length=1, max_length=3)

    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        schema = super().model_json_schema(*args, **kwargs)
        # Citations belong to the investigation's separate generation contract.
        # Preserve the original closed single-pass schema for strict providers.
        schema["$defs"]["Hypothesis"]["properties"].pop("evidence_refs", None)
        return schema

    @field_validator("hypotheses")
    @classmethod
    def ordered_ranks(cls, hypotheses: list[Hypothesis]) -> list[Hypothesis]:
        if any(item.evidence_refs for item in hypotheses):
            raise ValueError("Single-pass generation cannot cite uncollected investigation evidence")
        hypotheses = sorted(hypotheses, key=lambda item: item.rank)
        if [item.rank for item in hypotheses] != list(range(1, len(hypotheses) + 1)):
            raise ValueError("Hypothesis ranks must be unique and contiguous, starting at 1")
        return hypotheses
