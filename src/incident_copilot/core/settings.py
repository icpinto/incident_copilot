from pathlib import Path
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

    llm_provider: str = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "gpt-oss:20b"
    openai_compatible_base_url: str = "http://localhost:8000/v1"
    openai_compatible_model: str = "gpt-oss-20b"
    openai_compatible_api_key: str = "dummy"

    top_k_candidates: int = Field(default=10, ge=1)
    deep_analysis_top_n: int = Field(default=5, ge=1)

    def ensure_directories(self) -> None:
        (self.data_dir / "indexes").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "incidents").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "reports").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "temp_worktrees").mkdir(parents=True, exist_ok=True)
        self.index_db_path.parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_directories()
