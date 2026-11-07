"""Host collector inventory, version observations, and bounded error ingestion."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from incident_copilot.schemas.deployment import (
    DeploymentObservation,
    DeploymentPage,
    ReleaseMetadata,
    RuntimeConfiguration,
)
from incident_copilot.schemas.runtime import RuntimeCapability, RuntimeResult

from .deployments import context, observe
from .issue_capture import prepare_upload
from .models import IncidentSubmission
from .repository_source import repository_root
from .service import DashboardService
from .store import utcnow

router = APIRouter(tags=["log sources"])


def collector(request: Request) -> DashboardService:
    expected = os.environ.get("IC_LOG_INGEST_TOKEN", "")
    actual = request.headers.get("authorization", "")
    if not expected or not hmac.compare_digest(actual, f"Bearer {expected}"):
        raise HTTPException(401, "Collector authentication required")
    return request.app.state.dashboard


class ContainerObservation(BaseModel):
    project: str = Field(min_length=1, max_length=120)
    service: str = Field(min_length=1, max_length=120)
    container_id: str = Field(min_length=1, max_length=128)
    release: ReleaseMetadata = Field(default_factory=ReleaseMetadata)
    runtime_config: RuntimeConfiguration | None = None


class Inventory(BaseModel):
    collector_id: str = Field(default="legacy", min_length=1, max_length=128)
    projects: dict[str, list[str]] = Field(max_length=200)
    containers: list[ContainerObservation] = Field(default_factory=list, max_length=2000)
    runtime_version: int | None = None
    runtime_capabilities: list[RuntimeCapability] = Field(default_factory=list, max_length=5)


class SourceMapping(BaseModel):
    repository_id: str | None = None
    enabled: bool = False
    environment: str = Field(default="unknown", min_length=1, max_length=120)
    versioned_services: list[str] = Field(default_factory=list, max_length=200)
    runtime_enabled: bool = True
    runtime_dependencies: dict[str, list[str]] = Field(default_factory=dict, max_length=200)
    telemetry_profile: str | None = Field(default=None, max_length=120, pattern=r"^[A-Za-z0-9_.-]+$")


class LogBatch(ContainerObservation):
    collector_id: str = Field(default="legacy", min_length=1, max_length=128)
    batch_id: str = Field(min_length=1, max_length=128)
    log_text: str = Field(min_length=1, max_length=256_000)


def sources(service: DashboardService) -> list[dict]:
    with service.store.connection() as db:
        rows = db.execute("SELECT * FROM collector_sources ORDER BY collector_id,project").fetchall()
        hosts = {row["collector_id"]: {**json.loads(row["payload"]), "updated_at": row["updated_at"]}
                 for row in db.execute("SELECT * FROM runtime_collectors")}
    return [{**dict(row), "services": json.loads(row["services"]),
             "runtime_enabled": bool(row["runtime_enabled"]),
             "runtime_dependencies": json.loads(row["runtime_dependencies"]),
             "runtime_collector": hosts.get(row["collector_id"]),
             "versioned_services": json.loads(row["versioned_services"]), "enabled": bool(row["enabled"])}
            for row in rows]


@router.get("/log-sources")
def list_sources(request: Request):
    return sources(request.app.state.dashboard)


@router.put("/log-sources/{project}")
def map_source(project: str, mapping: SourceMapping, request: Request,
               collector_id: str = Query(default="legacy", min_length=1, max_length=128)):
    service = request.app.state.dashboard
    if any(not name or len(name) > 120 or len(dependencies) > 4 or
           any(not dependency or len(dependency) > 120 for dependency in dependencies)
           for name, dependencies in mapping.runtime_dependencies.items()):
        raise HTTPException(422, "Each runtime service supports up to four named dependencies")
    if mapping.enabled and not mapping.repository_id:
        raise HTTPException(422, "Choose a repository before enabling collection")
    if any(not name or len(name) > 120 for name in mapping.versioned_services):
        raise HTTPException(422, "Invalid service name")
    if mapping.repository_id:
        repo = service.store.get("repositories", mapping.repository_id)
        if not repo or repo.get("archived"):
            raise HTTPException(422, "Choose an active repository")
        if mapping.enabled and not mapping.versioned_services and not repository_root(repo):
            raise HTTPException(422, "Sync this repository before enabling collection")
    with service.store.connection() as db:
        previous = db.execute("SELECT * FROM collector_sources WHERE collector_id=? AND project=?", (collector_id, project)).fetchone()
        if previous:
            # Older clients retain the runtime policy when saving other source settings.
            if "runtime_enabled" not in mapping.model_fields_set:
                mapping.runtime_enabled = bool(previous["runtime_enabled"])
            if "runtime_dependencies" not in mapping.model_fields_set:
                mapping.runtime_dependencies = json.loads(previous["runtime_dependencies"])
            if "telemetry_profile" not in mapping.model_fields_set:
                mapping.telemetry_profile = previous["telemetry_profile"]
        changed = db.execute("""UPDATE collector_sources SET repository_id=?, enabled=?,
            environment=?, versioned_services=?,runtime_enabled=?,runtime_dependencies=?,telemetry_profile=?
            WHERE collector_id=? AND project=?""",
            (mapping.repository_id, int(mapping.enabled), mapping.environment.strip() or "unknown",
             json.dumps(sorted(set(mapping.versioned_services))), int(mapping.runtime_enabled),
             json.dumps(mapping.runtime_dependencies), mapping.telemetry_profile, collector_id, project))
        if not changed.rowcount:
            raise HTTPException(404, "Compose project has not been discovered")
        if not mapping.enabled or not mapping.runtime_enabled:
            db.execute("""UPDATE runtime_requests SET state='canceled',lease=NULL
                WHERE collector_id=? AND state IN ('queued','leased') AND json_extract(payload,'$.scope.project')=?""",
                (collector_id, project))
    return next(row for row in sources(service) if row["project"] == project and row["collector_id"] == collector_id)


@router.post("/collector/inventory", status_code=202)
def inventory(payload: Inventory, service: Annotated[DashboardService, Depends(collector)]):
    if any(not project or len(project) > 120 or len(names) > 200 or
           any(not name or len(name) > 120 for name in names)
           for project, names in payload.projects.items()):
        raise HTTPException(422, "Invalid Compose inventory")
    now = utcnow()
    try:
        with service.store.connection() as db:
            if payload.runtime_version == 1:
                from .runtime_evidence import register
                # Preserve the helper's live heartbeat when inventory refreshes.
                if not db.execute("SELECT 1 FROM runtime_collectors WHERE collector_id=?", (payload.collector_id,)).fetchone():
                    register(db, payload.collector_id, payload.runtime_capabilities)
            for project, names in payload.projects.items():
                db.execute("""INSERT INTO collector_sources(collector_id,project,services,last_seen)
                    VALUES (?,?,?,?) ON CONFLICT(collector_id,project) DO UPDATE
                    SET services=excluded.services,last_seen=excluded.last_seen""",
                    (payload.collector_id, project, json.dumps(sorted(set(names))), now))
            for item in payload.containers:
                observe(db, collector_id=payload.collector_id,
                        **item.model_dump(exclude={"release", "runtime_config"}),
                        release=item.release, runtime_config=item.runtime_config, now=now)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return {"projects": len(payload.projects)}


class RuntimeClaim(BaseModel):
    model_config = {"extra": "forbid"}
    collector_id: str = Field(min_length=1, max_length=128)
    capabilities: list[RuntimeCapability] = Field(max_length=5)
    helper: dict = Field(default_factory=dict)


class RuntimeDelivery(BaseModel):
    model_config = {"extra": "forbid"}
    collector_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    lease: str = Field(min_length=1, max_length=128)
    result: RuntimeResult


@router.post("/collector/runtime/claim")
async def runtime_claim(request: Request, service: Annotated[DashboardService, Depends(collector)]):
    from .runtime_evidence import claim
    try:
        payload = RuntimeClaim.model_validate(await bounded_body(request, 8192))
    except ValueError:
        raise HTTPException(422, "Invalid runtime claim") from None
    return claim(service.store, payload.collector_id, payload.capabilities, payload.helper)


async def bounded_body(request, maximum):
    from pydantic import ValidationError
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > maximum:
            raise HTTPException(413, "Runtime request is too large")
    try:
        return json.loads(data)
    except (ValueError, ValidationError):
        raise HTTPException(422, "Invalid runtime request") from None


@router.post("/collector/runtime/results")
async def runtime_results(request: Request, service: Annotated[DashboardService, Depends(collector)]):
    from pydantic import ValidationError

    from .runtime_evidence import accept
    try:
        payload = RuntimeDelivery.model_validate(await bounded_body(request, 12288))
        return accept(service.store, payload.collector_id, payload.request_id, payload.lease,
                      payload.result, service.configuration.redact)
    except (ValueError, ValidationError):
        raise HTTPException(422, "Invalid, expired, or out-of-scope runtime result") from None


@router.get("/deployments", response_model=DeploymentPage)
def list_deployments(request: Request, repository_id: str = "", service: str = "", environment: str = "",
                     collector_id: str = "", project: str = "", page: int = Query(1, ge=1),
                     page_size: int = Query(20, ge=1, le=100)):
    dashboard = request.app.state.dashboard
    query = """FROM deployments d JOIN collector_sources s
        ON d.collector_id=s.collector_id AND d.project=s.project
        WHERE (?='' OR s.repository_id=?) AND (?='' OR d.service=?)
          AND (?='' OR s.environment=?) AND (?='' OR d.collector_id=?) AND (?='' OR d.project=?)"""
    args = (repository_id, repository_id, service, service, environment, environment,
            collector_id, collector_id, project, project)
    with dashboard.store.connection() as db:
        db.execute("BEGIN")
        total = db.execute("SELECT COUNT(*) " + query, args).fetchone()[0]
        rows = db.execute("SELECT d.payload,s.repository_id,s.environment " + query +
            " ORDER BY json_extract(d.payload,'$.last_observed') DESC,d.id DESC LIMIT ? OFFSET ?",
            (*args, page_size, (page-1)*page_size)).fetchall()
    items = [json.loads(row["payload"]) | {"repository_id": row["repository_id"], "environment": row["environment"]}
             for row in rows]
    items = dashboard.configuration.redact({"items": items})["items"]
    return DeploymentPage(items=[DeploymentObservation.model_validate(item) for item in items],
                          total=total, page=page, page_size=page_size)


@router.post("/collector/logs", status_code=202)
def ingest(payload: LogBatch, service: Annotated[DashboardService, Depends(collector)]):
    with service.store.connection() as db:
        row = db.execute("SELECT * FROM collector_sources WHERE collector_id=? AND project=?",
                         (payload.collector_id, payload.project)).fetchone()
    if not row or not row["enabled"]:
        return {"accepted": 0, "reason": "source_disabled"}
    source = dict(row)
    repository_id = source["repository_id"]
    repo = service.store.get("repositories", repository_id)
    if not repo or repo.get("archived"):
        return {"accepted": 0, "reason": "repository_unavailable"}
    with service.store.connection() as db:
        try:
            observation = observe(db, collector_id=payload.collector_id, project=payload.project,
                service=payload.service, container_id=payload.container_id, release=payload.release,
                runtime_config=payload.runtime_config, now=utcnow())
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
    deployment = service.configuration.redact(context(observation, source, payload.release))
    versioned = payload.service in json.loads(source["versioned_services"])
    log_text = f"{payload.service} | {payload.log_text}"
    capture = prepare_upload(log_text, service.configuration.redact)
    accepted = investigations = 0
    release_key = [source["environment"], payload.service, deployment["revision"],
                   observation["release_identity"]]
    cooldown = hashlib.sha256(json.dumps([payload.collector_id, payload.project, release_key]).encode()).hexdigest()
    for item in capture["events"]:
        event = item["event"]
        event["deployment_context"] = deployment
        event_key = hashlib.sha256((f"{payload.project}:{payload.container_id}:{payload.batch_id}:{event['id']}"
            if payload.collector_id == "legacy" else json.dumps([payload.collector_id, payload.project,
                payload.container_id, payload.batch_id, event["id"]])).encode()).hexdigest()
        legacy_cooldown = payload.collector_id == "legacy" and not versioned and source["environment"] == "unknown" and not payload.release.revision
        delivery = {"event_id": event_key, "project": payload.project,
                    "cooldown_key": item["fingerprint"] if legacy_cooldown else cooldown + ":" + item["fingerprint"]}
        # Capture, deduplication, cooldown reservation, and incident creation commit
        # together. A crash cannot acknowledge an event without saving its evidence.
        service.submit(IncidentSubmission(repository_id=repository_id,
            title=f"{payload.service}: {event['message'].splitlines()[0][:150]}",
            log_text="\n".join(log_text.splitlines()[event["line_start"]-1:event["line_end"]]),
            user_hint_service=payload.service),
            deployment_context=deployment, version_linked=versioned,
            prepared_capture={"content_hash": event_key, "events": [item]}, collector_event=delivery)
        added, started = delivery["outcome"]
        accepted += added
        investigations += started
    return {"accepted": accepted, "investigations": investigations}
