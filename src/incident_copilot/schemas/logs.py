from typing import Literal

from pydantic import BaseModel, Field


class StackFrame(BaseModel):
    file_path: str | None = None
    line_number: int | None = None
    function_name: str | None = None
    raw: str


class LogException(BaseModel):
    id: str
    type: str
    message: str
    relation: Literal["caused_by", "during_handling", "cause"] | None = None
    previous_id: str | None = None
    stack_frames: list[StackFrame] = Field(default_factory=list)


class LogEvent(BaseModel):
    id: str
    line_start: int
    line_end: int
    message: str
    service: str | None = None
    timestamp: str | None = None
    timestamp_utc: str | None = None
    trace_id: str | None = None
    request_id: str | None = None
    error_bearing: bool = False
    exceptions: list[LogException] = Field(default_factory=list)
    stack_frames: list[StackFrame] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class IncidentGroup(BaseModel):
    id: str
    event_ids: list[str]
    services: list[str] = Field(default_factory=list)
    error_bearing: bool = False
    first_line: int
    grouping_basis: Literal["trace_id", "request_id", "record"]
    max_candidate_score: float = 0


class IncidentContext(BaseModel):
    version: Literal[1] = 1
    events: list[LogEvent] = Field(default_factory=list)
    groups: list[IncidentGroup] = Field(default_factory=list)
    primary_group_id: str | None = None
    selection_reason: str | None = None
    limitations: list[str] = Field(default_factory=list)


class ParsedLog(BaseModel):
    services: list[str] = []
    error_lines: list[str] = []
    exception_types: list[str] = []
    stack_frames: list[StackFrame] = []
    keywords: list[str] = []
    tables_or_entities: list[str] = []
    raw_text: str
    incident_context: IncidentContext | None = None
