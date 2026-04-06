from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field


class IncidentInput(BaseModel):
    repo_root: Path
    compose_path: Optional[Path] = None
    log_text: str = Field(min_length=1)
    incident_id: Optional[str] = None
    user_hint_service: Optional[str] = None
