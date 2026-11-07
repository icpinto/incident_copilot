"""Declared container provenance, kept separate from analyzed source identity."""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator


ENV_NAMES = {"ENV", "ENVIRONMENT", "APP_ENV", "NODE_ENV", "FLASK_ENV", "RAILS_ENV", "ASPNETCORE_ENVIRONMENT"}
BOOLEAN = {"true", "false", "1", "0", "yes", "no", "on", "off"}


def safe_config_value(name: str, value: str) -> bool:
    lower = value.lower()
    if name in ENV_NAMES:
        return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", value))
    if name == "LOG_LEVEL":
        return lower in {"trace", "debug", "info", "warn", "warning", "error", "fatal", "critical"}
    if name == "DEBUG" or name.startswith(("FEATURE_", "FF_")):
        return lower in BOOLEAN
    if name in {"PORT", "WORKERS"}:
        return bool(re.fullmatch(r"[0-9]{1,5}", value))
    if name == "HOST":
        return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,127}", value))
    return False


class RuntimeEnv(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
    value: str = Field(max_length=128)

    @field_validator("value")
    @classmethod
    def safe_value(cls, value: str, info):
        name = info.data.get("name", "")
        if value != "[redacted]" and not safe_config_value(name, value):
            raise ValueError("Only bounded, approved configuration values may be captured")
        return value


class RuntimeMount(BaseModel):
    destination: str = Field(min_length=1, max_length=512, pattern=r"^/[A-Za-z0-9_./-]*$")
    type: Literal["bind", "volume", "tmpfs", "other"]
    read_only: bool


class RuntimeConfiguration(BaseModel):
    status: Literal["available", "unavailable"] = "unavailable"
    environment: list[RuntimeEnv] = Field(default_factory=list, max_length=128)
    mounts: list[RuntimeMount] = Field(default_factory=list, max_length=64)
    truncated: bool = False


def analysis_configuration(snapshot: dict | None) -> dict | None:
    if not snapshot:
        return None
    environment = snapshot.get("environment") or []
    mounts = snapshot.get("mounts") or []
    return {"status": snapshot.get("status", "unavailable"),
            "environment": environment[:32], "mounts": mounts[:16],
            "truncated": bool(snapshot.get("truncated") or len(environment) > 32 or len(mounts) > 16)}


class ReleaseMetadata(BaseModel):
    revision: str | None = Field(default=None, max_length=256)
    version: str | None = Field(default=None, max_length=256)
    source_url: str | None = Field(default=None, max_length=2048)
    image_ref: str | None = Field(default=None, max_length=2048)
    image_id: str | None = Field(default=None, max_length=256)
    repo_digests: list[Annotated[str, Field(max_length=2048)]] = Field(default_factory=list, max_length=100)


class DeploymentContext(ReleaseMetadata):
    deployment_id: str
    collector_id: str
    project: str
    service: str
    container_id: str
    environment: str = "unknown"
    runtime_config: RuntimeConfiguration | None = None


class DeploymentObservation(DeploymentContext):
    repository_id: str | None = None
    first_observed: str
    last_observed: str


class DeploymentPage(BaseModel):
    items: list[DeploymentObservation]
    total: int
    page: int
    page_size: int


class SourceResolution(BaseModel):
    status: Literal["pending", "matched", "unavailable", "unknown"] = "unknown"
    requested_commit: str | None = None
    analyzed_commit: str | None = None
    reason: str | None = None
