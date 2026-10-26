"""Trusted, non-checkpointed dependencies for one investigation execution."""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path


class SourceSnapshotUnverifiedError(ValueError):
    """The indexed repository cannot be verified for iterative evidence retrieval."""


@dataclass(frozen=True)
class InvestigationRuntime:
    repo_root: Path
    index_db_path: Path
    execution_id: str
    progress: Callable[[dict], Awaitable[None]] | None = None
    heartbeat: Callable[[], None] | None = None
