"""Public contracts for uploaded error intelligence (independent of diagnosis)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from incident_copilot.schemas.logs import LogEvent

IssueStatus = Literal["open", "acknowledged", "investigating", "resolved", "ignored"]
Dimension = Literal["repository", "service", "error_class", "file", "function"]
IssueSort = Literal["occurrences", "growth", "last_seen"]


class IssueFilters(BaseModel):
    q: str = ""
    repository_id: str = ""
    service_name: str = ""
    error_class: str = ""
    file_path: str = ""
    function_name: str = ""
    owner: str = ""
    status: IssueStatus | None = None
    incident_id: str = ""
    start_at: datetime | None = None
    end_at: datetime | None = None


class IssueSummary(BaseModel):
    id: str
    repository_id: str
    repository_name: str
    title: str
    owner: str
    status: IssueStatus
    resolution_note: str
    resolved_at: str | None
    created_at: str
    updated_at: str
    first_seen: str | None
    last_seen: str | None
    occurrence_count: int
    previous_count: int
    total_count: int
    change: int
    change_percent: float | None
    source_timestamp_count: int
    fallback_timestamp_count: int


class IssueDetail(IssueSummary):
    redirected_from: str | None = None
    tracking_since: str


class IssuePage(BaseModel):
    items: list[IssueSummary]
    total: int
    page: int
    page_size: int
    start_at: str
    end_at: str
    tracking_since: str


class IssueUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    owner: str | None = Field(default=None, max_length=120)
    status: IssueStatus | None = None
    resolution_note: str | None = Field(default=None, max_length=4000)


class IssueMerge(BaseModel):
    source_issue_ids: list[str] = Field(min_length=1, max_length=100)


class IssueSplit(BaseModel):
    occurrence_ids: list[str] = Field(min_length=1, max_length=1000)
    title: str | None = Field(default=None, min_length=1, max_length=200)


class InvestigationLink(BaseModel):
    id: str
    title: str


class OccurrenceEvent(LogEvent):
    group_id: str | None = None


class ErrorOccurrence(BaseModel):
    id: str
    issue_id: str
    batch_id: str
    event_id: str
    effective_at: str
    timestamp_source: Literal["event", "submission"]
    event: OccurrenceEvent
    investigations: list[InvestigationLink]


class OccurrencePage(BaseModel):
    items: list[ErrorOccurrence]
    total: int
    page: int
    page_size: int


class IssueActivity(BaseModel):
    id: str
    issue_id: str
    kind: str
    created_at: str
    effective_at: str
    details: dict[str, Any]


class ActivityPage(BaseModel):
    items: list[IssueActivity]
    total: int
    page: int
    page_size: int


class IntelligenceSummary(BaseModel):
    occurrences: int
    previous_occurrences: int
    change: int
    change_percent: float | None
    issues: int
    active_issues: int
    new_issues: int
    reopened_issues: int
    source_timestamp_count: int
    fallback_timestamp_count: int


class TrendPoint(BaseModel):
    date: str
    occurrences: int


class Hotspot(BaseModel):
    value: str
    label: str
    occurrences: int


class ErrorIntelligence(BaseModel):
    tracking_since: str
    start_at: str
    end_at: str
    previous_start_at: str
    dimension: Dimension
    summary: IntelligenceSummary
    trends: list[TrendPoint]
    hotspots: list[Hotspot]
    frequent_issues: list[IssueSummary]
    growing_issues: list[IssueSummary]
