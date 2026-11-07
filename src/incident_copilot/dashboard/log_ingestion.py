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


class SourceMapping(BaseModel):
    repository_id: str | None = None
    enabled: bool = False
    environment: str = Field(default="unknown", min_length=1, max_length=120)
    versioned_services: list[str] = Field(default_factory=list, max_length=200)


class LogBatch(ContainerObservation):
    collector_id: str = Field(default="legacy", min_length=1, max_length=128)
    batch_id: str = Field(min_length=1, max_length=128)
    log_text: str = Field(min_length=1, max_length=256_000)


def sources(service: DashboardService) -> list[dict]:
    with service.store.connection() as db:
        rows = db.execute("SELECT * FROM collector_sources ORDER BY collector_id,project").fetchall()
    return [{**dict(row), "services": json.loads(row["services"]),
             "versioned_services": json.loads(row["versioned_services"]), "enabled": bool(row["enabled"])}
            for row in rows]


@router.get("/log-sources")
def list_sources(request: Request):
    return sources(request.app.state.dashboard)


@router.put("/log-sources/{project}")
def map_source(project: str, mapping: SourceMapping, request: Request,
               collector_id: str = Query(default="legacy", min_length=1, max_length=128)):
    service = request.app.state.dashboard
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
        changed = db.execute("""UPDATE collector_sources SET repository_id=?, enabled=?,
            environment=?, versioned_services=? WHERE collector_id=? AND project=?""",
            (mapping.repository_id, int(mapping.enabled), mapping.environment.strip() or "unknown",
             json.dumps(sorted(set(mapping.versioned_services))), collector_id, project))
        if not changed.rowcount:
            raise HTTPException(404, "Compose project has not been discovered")
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
