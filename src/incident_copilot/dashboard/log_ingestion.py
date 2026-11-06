"""Host collector inventory and bounded error ingestion."""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .issue_capture import capture_upload, prepare_upload
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


class Inventory(BaseModel):
    projects: dict[str, list[str]] = Field(max_length=200)


class SourceMapping(BaseModel):
    repository_id: str | None = None
    enabled: bool = False


class LogBatch(BaseModel):
    project: str = Field(min_length=1, max_length=120)
    service: str = Field(min_length=1, max_length=120)
    container_id: str = Field(min_length=1, max_length=128)
    batch_id: str = Field(min_length=1, max_length=128)
    log_text: str = Field(min_length=1, max_length=256_000)


def sources(service: DashboardService) -> list[dict]:
    with service.store.connection() as db:
        rows = db.execute("SELECT * FROM log_sources ORDER BY project").fetchall()
    import json
    return [{**dict(row), "services": json.loads(row["services"]), "enabled": bool(row["enabled"])}
            for row in rows]


@router.get("/log-sources")
def list_sources(request: Request):
    return sources(request.app.state.dashboard)


@router.put("/log-sources/{project}")
def map_source(project: str, mapping: SourceMapping, request: Request):
    service = request.app.state.dashboard
    if mapping.enabled and not mapping.repository_id:
        raise HTTPException(422, "Choose a repository before enabling collection")
    if mapping.repository_id:
        repo = service.store.get("repositories", mapping.repository_id)
        if not repo or repo.get("archived"):
            raise HTTPException(422, "Choose an active repository")
        if mapping.enabled and not repository_root(repo):
            raise HTTPException(422, "Sync this repository before enabling collection")
    with service.store.connection() as db:
        changed = db.execute("UPDATE log_sources SET repository_id=?, enabled=? WHERE project=?",
                             (mapping.repository_id, int(mapping.enabled), project))
        if not changed.rowcount:
            raise HTTPException(404, "Compose project has not been discovered")
    return next(row for row in sources(service) if row["project"] == project)


@router.post("/collector/inventory", status_code=202)
def inventory(payload: Inventory, service: DashboardService = Depends(collector)):
    import json
    now = utcnow()
    with service.store.connection() as db:
        for project, names in payload.projects.items():
            if not project or len(project) > 120 or len(names) > 200:
                raise HTTPException(422, "Invalid Compose inventory")
            db.execute("""INSERT INTO log_sources(project,services,last_seen) VALUES (?,?,?)
                ON CONFLICT(project) DO UPDATE SET services=excluded.services,last_seen=excluded.last_seen""",
                (project, json.dumps(sorted(set(names))), now))
    return {"projects": len(payload.projects)}


@router.post("/collector/logs", status_code=202)
def ingest(payload: LogBatch, service: DashboardService = Depends(collector)):
    with service.store.connection() as db:
        source = db.execute("SELECT repository_id,enabled FROM log_sources WHERE project=?",
                            (payload.project,)).fetchone()
    if not source or not source["enabled"]:
        return {"accepted": 0, "reason": "source_disabled"}
    repository_id = source["repository_id"]
    repo = service.store.get("repositories", repository_id)
    if not repo or repo.get("archived"):
        return {"accepted": 0, "reason": "repository_unavailable"}

    log_text = f"{payload.service} | {payload.log_text}"
    capture = prepare_upload(log_text, service.configuration.redact)
    accepted = investigations = 0
    for item in capture["events"]:
        event = item["event"]
        event_key = hashlib.sha256(f"{payload.project}:{payload.container_id}:{payload.batch_id}:{event['id']}".encode()).hexdigest()
        now = utcnow()
        with service.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM log_ingest_events WHERE event_id=?", (event_key,)).fetchone():
                continue
            db.execute("INSERT INTO log_ingest_events VALUES (?,?,?)", (event_key, payload.project, now))
            last = db.execute("SELECT last_started FROM log_investigation_cooldowns WHERE repository_id=? AND fingerprint=?",
                              (repository_id, item["fingerprint"])).fetchone()
            start = not last or datetime.fromisoformat(last["last_started"]) <= datetime.now(UTC) - timedelta(hours=1)
            if start:
                db.execute("""INSERT INTO log_investigation_cooldowns VALUES (?,?,?)
                    ON CONFLICT(repository_id,fingerprint) DO UPDATE SET last_started=excluded.last_started""",
                    (repository_id, item["fingerprint"], now))
        event_text = f"{payload.service} | {event['message']}"
        try:
            if start:
                service.submit(IncidentSubmission(repository_id=repository_id,
                    title=f"{payload.service}: {event['message'].splitlines()[0][:150]}",
                    log_text=event_text, user_hint_service=payload.service), capture_hash=event_key)
                investigations += 1
            else:
                with service.store.connection() as db:
                    capture_upload(db, {"repository_id": repository_id, "created_at": now},
                                   {"content_hash": event_key, "events": [item]}, link_incident=False)
            accepted += 1
        except Exception:
            with service.store.connection() as db:
                db.execute("DELETE FROM log_ingest_events WHERE event_id=?", (event_key,))
                if start:
                    db.execute("DELETE FROM log_investigation_cooldowns WHERE repository_id=? AND fingerprint=? AND last_started=?",
                               (repository_id, item["fingerprint"], now))
            raise
    return {"accepted": accepted, "investigations": investigations}
