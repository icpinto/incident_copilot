"""Versioned contracts for bounded, passive runtime observations."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RuntimeCapability = Literal["logs", "resources", "dependencies", "traces", "latency"]
FailureLayer = Literal["application", "database", "network", "infrastructure", "unknown"]
CAPABILITIES = ["logs", "resources", "dependencies", "traces", "latency"]


class RuntimeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuntimeScope(RuntimeRecord):
    version: Literal[1] = 1
    collector_id: str = Field(min_length=1, max_length=128)
    project: str = Field(min_length=1, max_length=120)
    environment: str = Field(max_length=120)
    service: str = Field(min_length=1, max_length=120)
    container_id: str = Field(min_length=1, max_length=128)
    anchor: datetime
    services: list[str] = Field(min_length=1, max_length=5)
    containers: dict[str, list[str]] = Field(default_factory=dict, max_length=200)
    trace_ids: list[str] = Field(default_factory=list, max_length=20)
    request_ids: list[str] = Field(default_factory=list, max_length=20)
    trigger_event_id: str | None = None
    telemetry_profile: str | None = Field(default=None, max_length=120)
    policy_version: str
    limitations: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def valid_scope(self):
        if self.anchor.tzinfo is None or self.service not in self.services:
            raise ValueError("Runtime scope requires an aware timestamp and the failing service")
        return self


class RuntimeQuery(RuntimeRecord):
    capability: RuntimeCapability
    service: str | None = Field(default=None, max_length=120)
    trace_id: str | None = Field(default=None, max_length=128)
    lookback_minutes: int = Field(default=5, ge=1, le=15, strict=True)


class RuntimeProvenance(RuntimeRecord):
    collector_id: str
    project: str
    environment: str
    service: str
    container_ids: list[str] = Field(default_factory=list, max_length=10)
    requested_start: datetime
    requested_end: datetime
    observed_at: datetime
    actual_start: datetime | None = None
    actual_end: datetime | None = None
    coverage: Literal["complete", "partial", "snapshot", "unavailable"] = "partial"

    @model_validator(mode="after")
    def valid_times(self):
        dates = [
            self.requested_start,
            self.requested_end,
            self.observed_at,
            self.actual_start,
            self.actual_end,
        ]
        if any(value is not None and value.tzinfo is None for value in dates):
            raise ValueError("Runtime timestamps must include a timezone")
        if self.requested_start > self.requested_end:
            raise ValueError("Invalid runtime time range")
        if self.actual_start and self.actual_end and self.actual_start > self.actual_end:
            raise ValueError("Invalid actual runtime time range")
        return self


class RuntimeResult(RuntimeRecord):
    capability: RuntimeCapability
    status: Literal["ok", "empty", "partial", "unavailable"]
    reason: str | None = Field(default=None, max_length=512)
    provenance: RuntimeProvenance
    data: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list, max_length=30)
    truncated: bool = False
    duration_ms: float = Field(default=0, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def actual_window(self):
        p = self.provenance
        if p.coverage != "snapshot" and any(
            value is not None and not p.requested_start <= value <= p.requested_end
            for value in (p.actual_start, p.actual_end)
        ):
            raise ValueError(
                "Historical runtime observations must fall within the requested window"
            )
        return self
