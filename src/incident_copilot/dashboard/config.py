from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from incident_copilot.core.settings import Settings

from .models import SettingsUpdate, SettingsView
from .store import ControlStore

SECRET = "openai_compatible_api_key"
SECRETS = {SECRET, "gitnexus_auth_token"}
INFRASTRUCTURE = {
    "app_name", "env", "data_dir", "index_db_path", "temporal_host", "temporal_namespace",
    "temporal_task_queue", "temporal_connect_max_attempts", "temporal_connect_retry_delay_seconds",
}


def control_path() -> Path:
    root = Path(__file__).resolve().parents[3]
    class BootstrapSettings(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="IC_", env_file=root / ".env", extra="ignore")
        control_db_path: Path = root / "data" / "control.db"

    value = BootstrapSettings().control_db_path.expanduser()
    return value if value.is_absolute() else root / value


def fingerprint(values: dict) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:16]


class Configuration:
    def __init__(self, store: ControlStore, *, activate: bool = True):
        self.store = store
        desired = self.desired().model_dump(mode="json")
        infrastructure = {key: desired[key] for key in INFRASTRUCTURE}
        self.infrastructure = (
            store.activate_infrastructure(infrastructure) if activate
            else store.config("infrastructure") or infrastructure
        )

    def desired(self) -> Settings:
        return Settings(**self.store.config("overrides"))

    def effective(self) -> Settings:
        return Settings(**(self.desired().model_dump() | self.infrastructure))

    def view(self) -> SettingsView:
        saved = self.desired().model_dump(mode="json")
        active = self.effective().model_dump(mode="json")
        configured = bool(saved[SECRET])
        graph_configured = bool(saved["gitnexus_auth_token"])
        for key in SECRETS:
            saved[key] = active[key] = ""
        return SettingsView(
            saved=saved, active=active, overridden=list(self.store.config("overrides")),
            restart_required=sorted(key for key in INFRASTRUCTURE if saved[key] != active[key]),
            infrastructure_fields=sorted(INFRASTRUCTURE), control_db_path=str(self.store.path),
            secret_configured=configured, graph_secret_configured=graph_configured,
        )

    def save(self, update: SettingsUpdate) -> SettingsView:
        unknown = (set(update.values) | set(update.reset)) - Settings.model_fields.keys()
        if unknown:
            raise ValueError("Unknown settings: " + ", ".join(sorted(unknown)))
        values = self.store.config("overrides")
        for key in update.reset:
            values.pop(key, None)
        # An empty secret field means keep the stored credential; reset explicitly clears overrides.
        values.update({key: value for key, value in update.values.items() if key not in SECRETS or value})
        try:
            validated = Settings(**values)
        except ValidationError:
            raise ValueError("Invalid configuration. Check field values and numeric limits.") from None
        if validated.llm_provider not in {"ollama", "openai_compatible"}:
            raise ValueError("Choose ollama or openai_compatible as the LLM provider.")
        if validated.deep_analysis_top_n > validated.top_k_candidates:
            raise ValueError("Deep analysis count cannot exceed the candidate limit.")
        from urllib.parse import urlparse
        for key in ("ollama_base_url", "openai_compatible_base_url", "gitnexus_url"):
            parsed = urlparse(getattr(validated, key))
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError("Provider endpoints must be HTTP(S) URLs without embedded credentials.")
        self.store.set_config("overrides", values)
        return self.view()

    def revision(self) -> str:
        payload = self.effective().model_dump(mode="json")
        revision = fingerprint(payload)
        self.store.put("revisions", revision, payload)
        return revision

    def redact(self, payload):
        """Remove configured credentials from all public payloads, including stored errors."""
        secrets = {getattr(self.desired(), key) for key in SECRETS}
        # Large log snapshots share the JSON table but contain no configuration secrets.
        secrets.update(row.get(key, "") for row in self.store.list(
            "revisions", exclude_id_prefix="incident-context-",
        ) for key in SECRETS)
        structural = {"id", "incident_id", "repository_id", "config_revision", "status", "stage", "engine", "created_at", "updated_at", "completed_at", "started_at"}
        def clean(value, key=""):
            if isinstance(value, dict):
                return {name: clean(item, name) for name, item in value.items()}
            if isinstance(value, list):
                return [clean(item, key) for item in value]
            if isinstance(value, str) and key not in structural:
                for secret in secrets:
                    if secret and secret != "dummy":
                        value = value.replace(secret, "[redacted]")
            return value
        return clean(payload)
