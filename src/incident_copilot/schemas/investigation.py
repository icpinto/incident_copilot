"""Validated, provider-neutral contracts for read-only incident investigation."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from incident_copilot.schemas.runtime import RuntimeCapability, RuntimeProvenance
from incident_copilot.schemas.validation import AnalysisValidationError

EvidenceToolName = Literal[
    "find_symbols", "read_source", "get_related_calls", "search_source", "get_file_context",
    "get_runtime_evidence",
]


class InvestigationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SymbolReference(InvestigationRecord):
    file_path: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1, max_length=256)
    line_start: int = Field(ge=1, strict=True)
    line_end: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def valid_range(self):
        if self.line_end < self.line_start:
            raise PydanticCustomError("invalid_line_range", "invalid_line_range")
        return self


class EvidenceRequest(InvestigationRecord):
    tool: EvidenceToolName
    question: str = Field(min_length=1, max_length=1000)
    hypothesis_id: str | None = Field(default=None, max_length=128)
    query: str | None = Field(default=None, min_length=1, max_length=512)
    file_path: str | None = Field(default=None, min_length=1, max_length=4096)
    symbol: SymbolReference | None = None
    line_start: int | None = Field(default=None, ge=1, strict=True)
    line_end: int | None = Field(default=None, ge=1, strict=True)
    direction: Literal["incoming", "outgoing", "both"] = "both"
    service: str | None = Field(default=None, max_length=256)
    language: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=20, ge=1, le=20, strict=True)
    capability: RuntimeCapability | None = None
    trace_id: str | None = Field(default=None, max_length=128)
    lookback_minutes: int = Field(default=5, ge=1, le=15, strict=True)

    @model_validator(mode="after")
    def valid_arguments(self):
        if self.tool == "get_runtime_evidence":
            if not self.capability or any((self.query, self.file_path, self.symbol,
                                           self.line_start, self.line_end, self.language)):
                raise ValueError("Runtime requests require a capability and cannot contain source/query arguments")
        elif self.capability or self.trace_id or self.lookback_minutes != 5:
            raise ValueError("Runtime arguments require get_runtime_evidence")
        if self.tool in {"find_symbols", "search_source"} and not self.query:
            raise PydanticCustomError("query_required", "query_required")
        if self.tool == "get_related_calls" and self.symbol is None:
            raise PydanticCustomError("symbol_required", "symbol_required")
        if self.tool == "get_file_context" and not self.file_path:
            raise PydanticCustomError("file_path_required", "file_path_required")
        if self.tool == "read_source" and self.symbol is None and not self.file_path:
            raise PydanticCustomError("source_location_required", "source_location_required")
        if self.line_end is not None and self.line_end < (self.line_start or 1):
            raise PydanticCustomError("invalid_line_range", "invalid_line_range")
        if self.symbol is not None and self.file_path and self.file_path != self.symbol.file_path:
            raise PydanticCustomError("conflicting_source_location", "conflicting_source_location")
        return self


class EvidenceItem(InvestigationRecord):
    id: str
    kind: str
    repository: str | None = None
    revision: str | None = None
    runtime: RuntimeProvenance | None = None
    file_path: str | None = None
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    content: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    request_id: str | None = None
    truncated: bool = False

    @model_validator(mode="after")
    def evidence_origin(self):
        if self.kind.startswith("runtime_") and self.kind != "runtime_config":
            if self.runtime is None or self.repository is not None or self.revision is not None:
                raise ValueError("Runtime evidence requires runtime provenance, not source identity")
        elif self.repository is None or self.revision is None:
            # Empty historical revision strings remain valid.
            raise ValueError("Source evidence requires repository and revision")
        return self


class ToolOutcome(InvestigationRecord):
    request_id: str
    tool: EvidenceToolName
    status: Literal["ok", "empty", "unavailable", "invalid"]
    reason: str | None = None
    evidence: list[EvidenceItem] = Field(default_factory=list)
    graph_context: dict[str, Any] | None = None
    duration_ms: float = Field(default=0, ge=0)
    validation_error: AnalysisValidationError | None = None


class InvestigationSummary(InvestigationRecord):
    mode: str = "agentic"
    version: int = 1
    rounds: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    elapsed_ms: float = Field(default=0, ge=0)
    stop_reason: str = ""
    incomplete: bool = False
    unresolved_gaps: list[str] = Field(default_factory=list)
    unassessed_request_ids: list[str] = Field(default_factory=list)


class InvestigationEvent(InvestigationRecord):
    id: str
    kind: str
    tool: EvidenceToolName | None = None
    question: str | None = None
    status: str
    reason: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    duration_ms: float = Field(default=0, ge=0)
