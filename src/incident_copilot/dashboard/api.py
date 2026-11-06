from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import date
from typing import Annotated
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import Response

from incident_copilot.analysis.graph_manager import repository_graph_status
from incident_copilot.reporting.markdown_renderer import render_markdown
from incident_copilot.schemas.code_graph import GraphPreparation
from incident_copilot.schemas.diagnosis import DiagnosisReport

from .config import control_path, fingerprint
from .models import (
    ConnectionCheck,
    ConnectionResult,
    IncidentPage,
    IncidentRecord,
    IncidentSubmission,
    Overview,
    RepositoryProfile,
    RepositorySync,
    RepositoryValidation,
    RepositoryWrite,
    SettingsUpdate,
    SettingsView,
)
from .repository_source import repository_root
from .service import DashboardService
from .store import ControlStore
from .issue_api import router as issue_router


async def reconciliation_loop(service: DashboardService):
    while True:
        try:
            await service.reconcile()
        except asyncio.CancelledError:
            raise
        except Exception:
            import logging
            logging.getLogger(__name__).warning("Dashboard reconciliation will retry after a control-store error.")
        await asyncio.sleep(2)


@asynccontextmanager
async def dashboard_lifespan(app: FastAPI):
    service = DashboardService(ControlStore(control_path()))
    app.state.dashboard = service
    task = asyncio.create_task(reconciliation_loop(service))
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def get_service(request: Request) -> DashboardService:
    return request.app.state.dashboard


router = APIRouter(prefix="/api", tags=["dashboard"])
router.include_router(issue_router)


def found(service: DashboardService, table: str, key: str) -> dict:
    result = service.store.get(table, key)
    if result is None:
        raise HTTPException(404, "Record not found")
    return result


@router.get("/repositories", response_model=list[RepositoryProfile])
def repositories(include_archived: bool = False, service: DashboardService = Depends(get_service)):
    return [service.public_repository(r) for r in service.store.list("repositories") if include_archived or not r["archived"]]


@router.post("/repositories", response_model=RepositoryProfile, status_code=201)
def create_repository(profile: RepositoryWrite, service: DashboardService = Depends(get_service)):
    return service.public_repository(service.store.save_repository(str(uuid4()), profile.model_dump(exclude={"github_token", "clear_github_token"}),
        token=profile.github_token.get_secret_value() if profile.github_token else "", clear_token=profile.clear_github_token))


@router.put("/repositories/{repository_id}", response_model=RepositoryProfile)
def update_repository(repository_id: str, profile: RepositoryWrite, service: DashboardService = Depends(get_service)):
    found(service, "repositories", repository_id)
    return service.public_repository(service.store.save_repository(repository_id, profile.model_dump(exclude={"github_token", "clear_github_token"}),
        token=profile.github_token.get_secret_value() if profile.github_token else "", clear_token=profile.clear_github_token))


def github_profile(repository_id: str, service: DashboardService) -> dict:
    profile = found(service, "repositories", repository_id)
    if profile.get("archived") or profile.get("source_type", "local") != "github":
        raise HTTPException(422, "Select an active GitHub repository to sync.")
    return profile


@router.post("/repositories/{repository_id}/sync", response_model=RepositorySync, status_code=202)
def sync_repository(repository_id: str, service: DashboardService = Depends(get_service)):
    github_profile(repository_id, service)
    try:
        return service.public_sync(service.request_sync(repository_id))
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@router.get("/repositories/{repository_id}/sync", response_model=RepositorySync)
def sync_status(repository_id: str, service: DashboardService = Depends(get_service)):
    github_profile(repository_id, service)
    return service.sync_status(repository_id)


@router.post("/repositories/{repository_id}/archive", response_model=RepositoryProfile)
def archive_repository(repository_id: str, service: DashboardService = Depends(get_service)):
    found(service, "repositories", repository_id)
    return service.public_repository(service.store.update("repositories", repository_id, {"archived": True}))


@router.post("/repositories/{repository_id}/validate", response_model=RepositoryValidation)
async def validate_repository(repository_id: str, service: DashboardService = Depends(get_service)):
    profile = found(service, "repositories", repository_id)
    root = repository_root(profile)
    if not root:
        return RepositoryValidation(valid=False, message="Check & sync this GitHub repository first.")
    try:
        result = await service.gateway.validate({"repo_root": root, "compose_path": profile.get("compose_path")})
    except Exception:
        raise HTTPException(503, "Worker validation is unavailable. Check Temporal and the worker connection.") from None
    service.store.update("repositories", repository_id, {"validation": result})
    return result


@router.post("/incidents", response_model=IncidentRecord, status_code=202)
def submit_incident(submission: IncidentSubmission, service: DashboardService = Depends(get_service)):
    try:
        return service.submit(submission)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@router.get("/repositories/{repository_id}/graph", response_model=GraphPreparation)
async def graph_status(repository_id: str, service: Annotated[DashboardService, Depends(get_service)]):
    profile = found(service, "repositories", repository_id)
    root = repository_root(profile)
    if not root:
        return GraphPreparation(repository="", status="not_prepared", reason="repository_unavailable")
    result = await repository_graph_status(root, service.configuration.effective())
    return result.model_copy(update={"repository": profile["remote_url"]}) if profile.get("source_type") == "github" else result


@router.post("/repositories/{repository_id}/graph/refresh", response_model=GraphPreparation, status_code=202)
async def refresh_graph(repository_id: str, service: Annotated[DashboardService, Depends(get_service)]):
    profile = found(service, "repositories", repository_id)
    if profile.get("archived"):
        raise HTTPException(422, "Archived repositories cannot be prepared.")
    root = repository_root(profile)
    if not root:
        raise HTTPException(422, "Check & sync this GitHub repository first.")
    result = await repository_graph_status(root, service.configuration.effective(), refresh=True)
    return result.model_copy(update={"repository": profile["remote_url"]}) if profile.get("source_type") == "github" else result


@router.get("/incidents", response_model=IncidentPage)
def list_incidents(
    q: str = "", repository_id: str = "", status: str = "", service_name: str = "", error_class: str = "",
    start_date: date | None = None, end_date: date | None = None,
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    service: DashboardService = Depends(get_service),
):
    rows = []
    for row in service.store.list("incidents"):
        report = row.get("report") or {}
        if repository_id and row["repository_id"] != repository_id:
            continue
        if status and row["status"] != status:
            continue
        if service_name.casefold() not in (report.get("likely_service") or "").casefold():
            continue
        if error_class.casefold() not in (report.get("likely_error_class") or "").casefold():
            continue
        if start_date and row["created_at"][:10] < start_date.isoformat():
            continue
        if end_date and row["created_at"][:10] > end_date.isoformat():
            continue
        if q and q.casefold() not in " ".join([row["id"], row["title"], row["repository_name"], report.get("summary", "")]).casefold():
            continue
        rows.append(row)
    return IncidentPage(items=[service.public(row) for row in rows[(page-1)*page_size:page*page_size]], total=len(rows), page=page, page_size=page_size)


@router.get("/incidents/{incident_id}", response_model=IncidentRecord)
def get_incident(incident_id: str, service: DashboardService = Depends(get_service)):
    return service.public(found(service, "incidents", incident_id))


@router.post("/incidents/{incident_id}/cancel", response_model=IncidentRecord)
async def cancel_incident(incident_id: str, service: DashboardService = Depends(get_service)):
    found(service, "incidents", incident_id)
    try:
        return await service.cancel(incident_id)
    except Exception:
        raise HTTPException(503, "Cancellation could not reach Temporal. The run remains saved; retry when the connection returns.") from None


@router.post("/incidents/{incident_id}/rerun", response_model=IncidentRecord, status_code=202)
def rerun_incident(incident_id: str, service: DashboardService = Depends(get_service)):
    run = found(service, "incidents", incident_id)
    try:
        return service.submit(IncidentSubmission.model_validate(run["input"] | {"title": run["title"]}), original=run)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@router.get("/incidents/{incident_id}/export")
def export_incident(incident_id: str, format: str = Query("json", pattern="^(json|markdown)$"), service: DashboardService = Depends(get_service)):
    record = service.public(found(service, "incidents", incident_id))
    if not record.report:
        raise HTTPException(409, "A diagnosis report is not available yet.")
    if format == "markdown":
        return Response(render_markdown(DiagnosisReport.model_validate(record.report)), media_type="text/markdown", headers={"Content-Disposition": f'attachment; filename="incident-{incident_id}.md"'})
    import json
    return Response(json.dumps(record.report.model_dump(mode="json"), indent=2), media_type="application/json", headers={"Content-Disposition": f'attachment; filename="incident-{incident_id}.json"'})


@router.get("/overview", response_model=Overview)
def overview(service: DashboardService = Depends(get_service)):
    return service.overview()


@router.get("/settings", response_model=SettingsView)
def settings(service: DashboardService = Depends(get_service)):
    return service.configuration.view()


@router.put("/settings", response_model=SettingsView)
def save_settings(update: SettingsUpdate, service: DashboardService = Depends(get_service)):
    try:
        return service.configuration.save(update)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@router.post("/connections/check", response_model=ConnectionResult)
async def check_connection(check: ConnectionCheck, service: DashboardService = Depends(get_service)):
    config = service.configuration.effective()
    try:
        if check.target == "temporal":
            await service.gateway.client()
            workers = service.store.list("workers")
            from datetime import datetime, timezone
            ready = any(
                (datetime.now(timezone.utc) - datetime.fromisoformat(w["updated_at"])).total_seconds() < 20
                and w["infrastructure"] == fingerprint(service.configuration.infrastructure)
                for w in workers
            )
            return ConnectionResult(ok=ready, message="Temporal and worker are connected." if ready else "Temporal is connected; waiting for a worker with matching active settings.")
        if check.target == "storage":
            directory = config.index_db_path.parent
            directory.mkdir(parents=True, exist_ok=True)
            import tempfile
            with tempfile.TemporaryFile(dir=directory) as probe:
                probe.write(b"storage-check")
            return ConnectionResult(ok=True, message="Control store and active index storage are writable on the API host.")
        url = config.ollama_base_url.rstrip("/") + "/api/tags" if config.llm_provider == "ollama" else config.openai_compatible_base_url.rstrip("/") + "/models"
        headers = {} if config.llm_provider == "ollama" else {"Authorization": f"Bearer {config.openai_compatible_api_key}"}
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
        return ConnectionResult(ok=True, message="LLM endpoint is reachable with the saved credentials.")
    except Exception:
        return ConnectionResult(ok=False, message=f"{check.target.title()} connection failed. Check active settings and service availability.")


