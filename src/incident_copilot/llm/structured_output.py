from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from incident_copilot.llm.validation import SEMANTIC_MESSAGES
from incident_copilot.schemas.diagnosis import Hypothesis
from incident_copilot.schemas.runtime import FailureLayer


class GeneratedHypothesis(BaseModel):
    """Model self-assessments are raw scores, never report probabilities."""
    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1, le=3)
    failure_layer: FailureLayer = "unknown"
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    title: str
    explanation: str
    evidence: list[str]
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    next_checks: list[str]


def report_hypothesis(raw: dict, evidence_refs: list[str] | None = None) -> dict:
    return Hypothesis(
        **{key: raw[key] for key in ("rank", "title", "explanation", "evidence", "next_checks")},
        confidence=None, confidence_status="unavailable", model_score=raw["confidence"],
        failure_layer=raw.get("failure_layer", "unknown"),
        evidence_refs=evidence_refs if evidence_refs is not None else raw.get("evidence_refs", []),
    ).model_dump(mode="json")


class HypothesesResponse(BaseModel):
    """The shared generation and validation contract for both LLM adapters."""

    model_config = ConfigDict(extra="forbid")

    diagnosis_status: Literal["provisional", "insufficient_evidence"]
    diagnosis_reason: str = Field(min_length=1)
    next_checks: list[str]
    hypotheses: list[GeneratedHypothesis] = Field(min_length=0, max_length=3)

    @field_validator("hypotheses")
    @classmethod
    def ordered_ranks(cls, hypotheses: list[GeneratedHypothesis]) -> list[GeneratedHypothesis]:
        hypotheses = sorted(hypotheses, key=lambda item: item.rank)
        if [item.rank for item in hypotheses] != list(range(1, len(hypotheses) + 1)):
            raise PydanticCustomError("invalid_ranks", SEMANTIC_MESSAGES["invalid_ranks"])
        return hypotheses

    @model_validator(mode="after")
    def coherent_outcome(self):
        if self.diagnosis_status == "provisional" and not self.hypotheses:
            raise PydanticCustomError("provisional_requires_hypothesis", SEMANTIC_MESSAGES["provisional_requires_hypothesis"])
        return self


def hypotheses_schema():
    """Strict provider schema; parsing still accepts defaults in historical responses."""
    schema = HypothesesResponse.model_json_schema()
    def visit(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(schema)
    return schema
