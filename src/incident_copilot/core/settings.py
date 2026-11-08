import tempfile
import warnings
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from incident_copilot.schemas.reasoning import ReasoningMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IC_", env_file=".env", extra="ignore")

    app_name: str = "incident-copilot"
    env: str = "dev"
    data_dir: Path = Path("./data")
    index_db_path: Path = Path("./data/indexes/repo_index.db")
    repository_cache_dir: Path | None = None

    temporal_host: str = "temporal:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "incident-copilot"
    temporal_connect_max_attempts: int = Field(default=60, ge=1)
    temporal_connect_retry_delay_seconds: int = Field(default=2, ge=1)

    llm_provider: str = "openai_compatible"
    llm_reasoning_mode: ReasoningMode = "baseline"
    ollama_base_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "llama3.1:8b"
    openai_compatible_base_url: str = "http://localhost:4000/v1"
    openai_compatible_model: str = "incident-diagnosis"
    openai_compatible_api_key: str = ""

    top_k_candidates: int = Field(default=10, ge=1)
    deep_analysis_top_n: int = Field(default=5, ge=1)

    agentic_enabled: bool = False
    agentic_max_rounds: int = Field(default=3, ge=1, le=3)
    agentic_max_tool_requests: int = Field(default=12, ge=1, le=12)
    agentic_timeout_seconds: float = Field(default=180, gt=0, le=180)

    guardrails_mode: Literal["off", "observe", "enforce"] = "observe"
    guardrails_timeout_seconds: float = Field(default=10, gt=0, le=180)

    code_graph_enabled: bool = True
    code_graph_mode: Literal["managed", "external"] = "managed"
    code_graph_prepare_timeout_seconds: float = Field(default=300, gt=0, le=300)
    gitnexus_url: str = "http://localhost:3111/mcp"
    # Explicit indexed path on the GitNexus server; empty selects the local repository path.
    gitnexus_repository: str = ""
    gitnexus_auth_token: str = ""
    code_graph_timeout_seconds: float = Field(default=15, gt=0, le=15)

    def model_post_init(self, __context: object, /) -> None:
        from urllib.parse import urlsplit
        endpoint = urlsplit(self.gitnexus_url)
        if endpoint.scheme not in {"http", "https"} or not endpoint.netloc or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError("GitNexus endpoint must be an HTTP(S) URL without credentials, query, or fragment.")
        if self.gitnexus_repository.startswith("@"):
            raise ValueError("Code graph evidence supports one indexed repository, not repository groups.")
        project_root = Path(__file__).resolve().parents[3]
        self.data_dir = self._normalize_path(self.data_dir, project_root)
        self.index_db_path = self._normalize_path(self.index_db_path, project_root)
        self.repository_cache_dir = self._normalize_path(self.repository_cache_dir, project_root) if self.repository_cache_dir else self.data_dir / "repository-cache"

    @staticmethod
    def _normalize_path(path_value: Path, project_root: Path) -> Path:
        expanded = path_value.expanduser()
        return expanded if expanded.is_absolute() else (project_root / expanded)

    def ensure_directories(self) -> None:
        try:
            self._ensure_directories_once()
        except OSError:
            fallback_root = Path(tempfile.gettempdir()) / "incident-copilot"
            self.data_dir = fallback_root
            self.index_db_path = fallback_root / "indexes" / "repo_index.db"
            warnings.warn(
                f"Configured data directory is not writable. Falling back to {fallback_root}. "
                "Set IC_INDEX_DB_PATH to override this location.",
                RuntimeWarning,
                stacklevel=2,
            )
            self._ensure_directories_once()

    def _ensure_directories_once(self) -> None:
        (self.data_dir / "indexes").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "incidents").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "reports").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "temp_worktrees").mkdir(parents=True, exist_ok=True)
        self.index_db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.index_db_path.exists() and self.index_db_path.is_dir():
            raise ValueError(f"index_db_path must be a file path, not a directory: {self.index_db_path}")
        self.index_db_path.touch(exist_ok=True)


settings = Settings()
settings.ensure_directories()
