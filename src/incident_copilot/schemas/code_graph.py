"""Provider-neutral, bounded static code evidence. Scores never enter this contract."""
from typing import Literal

from pydantic import BaseModel, Field


class GraphSymbol(BaseModel):
    uid: str = Field(min_length=1, max_length=1024)
    name: str = Field(min_length=1, max_length=256)
    file_path: str = Field(min_length=1, max_length=4096)
    kind: str = Field(default="unknown", max_length=64)
    line_start: int | None = Field(default=None, ge=1, strict=True)
    line_end: int | None = Field(default=None, ge=1, strict=True)


class GraphRelationship(BaseModel):
    source_uid: str
    target_uid: str
    relationship: Literal["CALLS"] = "CALLS"


class GraphSeedResult(BaseModel):
    file_path: str
    symbol_name: str
    status: Literal["found", "skipped"]
    reason: str | None = None


class GraphContext(BaseModel):
    provider: str = "gitnexus"
    provider_version: str | None = None
    repository: str
    repo_revision: str | None = None
    indexed_revision: str | None = None
    indexed_at: str | None = None
    status: Literal["available", "partial", "skipped", "unavailable"]
    reason: str | None = None
    # Even an exact provider result has bounded reference windows. Never claim completeness.
    completeness: Literal["bounded", "lower_bound", "unknown"] = "unknown"
    symbols: list[GraphSymbol] = Field(default_factory=list)
    relationships: list[GraphRelationship] = Field(default_factory=list)
    seeds: list[GraphSeedResult] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    retrieval_ms: float = 0

