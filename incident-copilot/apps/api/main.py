from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI

from incident_copilot.core.logging import configure_logging
from incident_copilot.core.settings import settings
from incident_copilot.schemas.incident import IncidentInput
from incident_copilot.workflows.activity_runner import run
from incident_copilot.workflows.temporal_activities import (
    build_service_map_and_index_activity,
    correlate_logs_activity,
    deep_analysis_activity,
    diagnosis_graph_activity,
)

configure_logging()
app = FastAPI(title="Incident Copilot API")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/diagnose")
async def diagnose(incident: IncidentInput) -> dict:
    await build_service_map_and_index_activity(str(incident.repo_root), str(incident.compose_path) if incident.compose_path else None)
    correlation = await correlate_logs_activity(incident.model_dump(mode="json"))
    deep = await deep_analysis_activity(str(incident.repo_root), correlation["candidates"])
    report = await diagnosis_graph_activity(
        incident.incident_id or str(uuid4()),
        correlation["parsed_log"],
        correlation["candidates"],
        deep,
    )
    return report
