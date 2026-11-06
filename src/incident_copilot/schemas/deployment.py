"""Declared container provenance, kept separate from analyzed source identity."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field


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
