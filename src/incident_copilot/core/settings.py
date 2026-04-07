from pathlib import Path
import tempfile
import warnings

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IC_", env_file=".env", extra="ignore")

    app_name: str = "incident-copilot"
    env: str = "dev"
    data_dir: Path = Path("./data")
    index_db_path: Path = Path("./data/indexes/repo_index.db")

    temporal_host: str = "temporal:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "incident-copilot"
    temporal_connect_max_attempts: int = Field(default=60, ge=1)
    temporal_connect_retry_delay_seconds: int = Field(default=2, ge=1)

    llm_provider: str = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "gpt-oss:20b"
    openai_compatible_base_url: str = "http://localhost:8000/v1"
    openai_compatible_model: str = "gpt-oss-20b"
    openai_compatible_api_key: str = "dummy"

    top_k_candidates: int = Field(default=10, ge=1)
    deep_analysis_top_n: int = Field(default=5, ge=1)

    def model_post_init(self, __context: object) -> None:
        project_root = Path(__file__).resolve().parents[3]
        self.data_dir = self._normalize_path(self.data_dir, project_root)
        self.index_db_path = self._normalize_path(self.index_db_path, project_root)

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
