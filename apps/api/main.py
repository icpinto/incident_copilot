from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from incident_copilot.core.logging import configure_logging
from incident_copilot.dashboard.api import dashboard_lifespan, router
from incident_copilot.observability.runtime import entrypoint
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
    lifespan=dashboard_lifespan,
    title="Incident Copilot API",
    version="1.0.0",
    summary="Incident diagnosis API designed for agentic and human clients.",
    description=(
        "HTTP API for log correlation and root-cause diagnosis.\n\n"
        "This OpenAPI contract includes explicit operation IDs, typed request/response models, "
        "and examples so LLM agents can reliably plan tool calls and parse responses."
    ),
)
app.include_router(router)


@app.exception_handler(RequestValidationError)
async def request_validation_error(request: Request, error: RequestValidationError):
    return JSONResponse({"detail": [
        {"loc": item["loc"], "msg": item["msg"], "type": item["type"]}
        for item in error.errors()
    ]}, status_code=422)


@app.middleware("http")
async def local_dashboard_origin(request: Request, call_next):
    # The local console has no login. Reject browser writes from unrelated sites.
    origin = request.headers.get("origin")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
        parsed = urlparse(origin)
        if parsed.netloc != request.headers.get("host") and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            return JSONResponse({"detail": "This local dashboard only accepts same-origin requests."}, status_code=403)
    return await call_next(request)


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
@entrypoint
async def diagnose(incident: IncidentInput, request: Request) -> DiagnosisReport:
    config = request.app.state.dashboard.configuration.effective()
    runtime = config.model_dump(mode="json")
    from incident_copilot.observability.runtime import current_execution_id, execution_metadata
    from incident_copilot.dashboard.config import fingerprint
    execution_id = current_execution_id() or str(uuid4())
    incident_id = incident.incident_id or execution_id
    execution_metadata(config, incident_id=incident_id, config_revision=fingerprint(runtime))
    context = {"execution_id": execution_id, "repo_root": str(incident.repo_root.resolve())}
    await build_service_map_and_index_activity(
        str(incident.repo_root),
        str(incident.compose_path) if incident.compose_path else None,
        runtime,
        context,
    )
    correlation = await correlate_logs_activity(incident.model_dump(mode="json"), runtime, context)
    deep = await deep_analysis_activity(str(incident.repo_root), correlation["candidates"], runtime, context)
    report = await diagnosis_graph_activity(
        incident_id,
        correlation["parsed_log"],
        correlation["candidates"],
        deep,
        runtime,
        context,
    )
    return DiagnosisReport.model_validate(report)


WEB_DIST = Path(__file__).resolve().parents[1] / "web" / "dist"
if (WEB_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="web-assets")


@app.get("/{path:path}", include_in_schema=False)
async def dashboard_page(path: str):
    if path == "api" or path.startswith("api/") or path.startswith("assets/"):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    if (WEB_DIST / "index.html").is_file():
        return FileResponse(WEB_DIST / "index.html")
    return JSONResponse({"detail": "Build the dashboard: cd apps/web && npm ci && npm run build"}, status_code=404)
