from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from pydantic import BaseModel

from incident_copilot.core.logging import configure_logging
from incident_copilot.schemas.diagnosis import DiagnosisReport
from incident_copilot.schemas.incident import IncidentInput
from incident_copilot.workflows.temporal_activities import (
    build_service_map_and_index_activity,
    correlate_logs_activity,
    deep_analysis_activity,
    diagnosis_graph_activity,
)


class HealthResponse(BaseModel):
    status: str


configure_logging()
app = FastAPI(
    title="Incident Copilot API",
    version="1.0.0",
    summary="Incident diagnosis API designed for agentic and human clients.",
    description=(
        "HTTP API for log correlation and root-cause diagnosis.\n\n"
        "This OpenAPI contract includes explicit operation IDs, typed request/response models, "
        "and examples so LLM agents can reliably plan tool calls and parse responses."
    ),
)


@app.get(
    "/health",
    tags=["system"],
    operation_id="get_health",
    summary="Health check",
    description="Lightweight liveness probe for orchestrators and agents.",
    response_model=HealthResponse,
    response_description="Service status payload.",
    responses={
        200: {
            "description": "API is available.",
            "content": {"application/json": {"example": {"status": "ok"}}},
        }
    },
)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post(
    "/diagnose",
    tags=["diagnosis"],
    operation_id="post_diagnose",
    summary="Run full incident diagnosis",
    description=(
        "Runs the full diagnosis pipeline: index/build service map, correlate log evidence, "
        "perform deep repository analysis, and generate ranked hypotheses."
    ),
    response_model=DiagnosisReport,
    response_description="Structured diagnosis report with ranked candidates and hypotheses.",
    responses={
        200: {
            "description": "Diagnosis completed successfully.",
            "content": {
                "application/json": {
                    "example": {
                        "incident_id": "8a27774d-6f8c-4451-a915-3f8d9f0bf658",
                        "likely_service": "payments-api",
                        "likely_error_class": "TimeoutError",
                        "candidates": [
                            {
                                "file_path": "services/payments/processor.py",
                                "symbol_name": "charge_card",
                                "service_name": "payments-api",
                                "score": 0.93,
                                "reasons": [
                                    "Stacktrace maps to service/payments module",
                                    "Error message matched timeout handling path",
                                ],
                                "snippet": "raise TimeoutError('payment gateway timeout')",
                                "line_start": 112,
                                "line_end": 112,
                            }
                        ],
                        "hypotheses": [
                            {
                                "rank": 1,
                                "title": "Gateway timeout propagates without retry",
                                "explanation": "Requests to downstream gateway exceed timeout and bubble up.",
                                "evidence": [
                                    "TimeoutError appears in logs",
                                    "No retry branch in charge_card",
                                ],
                                "confidence": 0.88,
                                "next_checks": [
                                    "Validate gateway latency metrics",
                                    "Add bounded retry in charge_card",
                                ],
                            }
                        ],
                        "summary": "Most likely incident source is payments-api timeout path.",
                        "engine": "deterministic+llm",
                    }
                }
            },
        }
    },
)
async def diagnose(incident: IncidentInput) -> DiagnosisReport:
    await build_service_map_and_index_activity(
        str(incident.repo_root),
        str(incident.compose_path) if incident.compose_path else None,
    )
    correlation = await correlate_logs_activity(incident.model_dump(mode="json"))
    deep = await deep_analysis_activity(str(incident.repo_root), correlation["candidates"])
    report = await diagnosis_graph_activity(
        incident.incident_id or str(uuid4()),
        correlation["parsed_log"],
        correlation["candidates"],
        deep,
    )
    return DiagnosisReport.model_validate(report)
