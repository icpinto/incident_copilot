from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .config import Configuration, fingerprint
from .models import IncidentRecord, IncidentSubmission, Overview
from .store import ControlStore, utcnow

TERMINAL = {"completed", "failed", "canceled"}


class TemporalGateway:
    def __init__(self, configuration: Configuration):
        self.configuration = configuration
        self.clients = {}

    async def client(self):
        from temporalio.client import Client
        config = self.configuration.effective()
        key = (config.temporal_host, config.temporal_namespace)
        if key not in self.clients:
            self.clients[key] = await asyncio.wait_for(
                Client.connect(key[0], namespace=key[1]), timeout=5,
            )
        return self.clients[key]

    async def start(self, incident_id: str):
        from temporalio.exceptions import WorkflowAlreadyStartedError
        from temporalio.common import WorkflowIDReusePolicy
        from incident_copilot.workflows.temporal_workflows import DiagnoseIncidentWorkflow
        client = await self.client()
        try:
            await client.start_workflow(
                DiagnoseIncidentWorkflow.run, {"dashboard": {"incident_id": incident_id}},
                id=f"incident-{incident_id}", task_queue=self.configuration.effective().temporal_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                execution_timeout=timedelta(hours=1),
            )
        except WorkflowAlreadyStartedError:
            pass  # Start acknowledgement may have been lost before the API restarted.

    async def inspect(self, incident_id: str) -> tuple[str, dict | None]:
        client = await self.client()
        handle = client.get_workflow_handle(f"incident-{incident_id}")
        description = await handle.describe()
        status = description.status.name
        progress = None
        if status == "RUNNING":
            try:
                progress = await asyncio.wait_for(handle.query("get_progress"), timeout=2)
            except Exception:
                # Persisted activity progress remains available while a worker is offline.
                pass
        if status == "COMPLETED":
            await handle.result()
        if description.close_time:
            progress = {"completed_at": description.close_time.isoformat()}
        return status, progress

    async def validate(self, profile: dict) -> dict:
        from incident_copilot.workflows.temporal_workflows import ValidateRepositoryWorkflow
        client = await self.client()
        return await asyncio.wait_for(client.execute_workflow(
            ValidateRepositoryWorkflow.run, profile, id=f"validate-{uuid4()}",
            task_queue=self.configuration.effective().temporal_task_queue,
            execution_timeout=timedelta(seconds=30),
        ), timeout=32)

    async def cancel(self, incident_id: str):
        client = await self.client()
        await asyncio.wait_for(client.get_workflow_handle(f"incident-{incident_id}").cancel(), timeout=5)


class DashboardService:
    def __init__(self, store: ControlStore):
        self.store = store
        self.configuration = Configuration(store)
        self.gateway = TemporalGateway(self.configuration)
        self.lock = asyncio.Lock()

    def public(self, payload: dict) -> IncidentRecord:
        return IncidentRecord.model_validate(self.configuration.redact(payload))

    def submit(self, submission: IncidentSubmission, *, original: dict | None = None) -> IncidentRecord:
        profile = self.store.get("repositories", submission.repository_id)
        if profile is None or profile.get("archived"):
            raise ValueError("Select an active repository profile.")
        if not submission.log_text.strip():
            raise ValueError("Provide non-empty incident logs.")
        incident_id = str(uuid4())
        config = self.configuration.effective()
        inputs = submission.model_dump() | {
            "repo_root": profile["repo_root"], "compose_path": profile.get("compose_path"),
        }
        if original:
            inputs = original["input"] | submission.model_dump()
        # A unique path also prevents stale files after a repository changes.
        index_path = config.index_db_path.parent / "runs" / incident_id / config.index_db_path.name
        record = {
            "id": incident_id, "title": submission.title.strip() or f"Incident {incident_id[:8]}",
            "repository_id": profile["id"], "repository_name": profile["name"],
            "status": "queued", "stage": "queued", "created_at": utcnow(), "updated_at": utcnow(),
            "completed_at": None, "config_revision": self.configuration.revision(),
            "index_db_path": str(index_path), "input": inputs, "timeline": [], "report": None,
            "artifact_dir": str(config.data_dir / "incidents" / incident_id),
            "evidence": {"raw_log": submission.log_text}, "error": None, "connection_error": None,
            "dispatched": False,
        }
        self.store.create_incident(record, self.configuration.infrastructure)
        return self.public(record)

    async def cancel(self, incident_id: str) -> IncidentRecord:
        async with self.lock:
            run = self.store.get("incidents", incident_id)
            if run is None:
                raise KeyError(incident_id)
            if run["status"] in TERMINAL:
                return self.public(run)
            if not run.get("dispatched"):
                return self.public(self.store.update("incidents", incident_id, {
                    "status": "canceled", "completed_at": utcnow(), "connection_error": None,
                }))
            await self.gateway.cancel(incident_id)
            return self.public(self.store.update("incidents", incident_id, {
                "connection_error": "Cancellation requested. Waiting for the worker to acknowledge it.",
            }))

    async def reconcile(self):
        if self.lock.locked():
            return
        async with self.lock:
            for run in self.store.list("incidents"):
                if run["status"] in TERMINAL:
                    continue
                try:
                    if not run.get("dispatched"):
                        workers = self.store.list("workers")
                        recent = [w for w in workers if (datetime.now(timezone.utc) - datetime.fromisoformat(w["updated_at"])).total_seconds() < 20]
                        own = fingerprint(self.configuration.infrastructure)
                        if recent and not any(w["infrastructure"] == own for w in recent):
                            raise RuntimeError("Worker configuration has not been restarted")
                        await asyncio.wait_for(self.gateway.start(run["id"]), timeout=8)
                        self.store.update("incidents", run["id"], {"dispatched": True})
                    status, progress = await asyncio.wait_for(self.gateway.inspect(run["id"]), timeout=8)
                    current = self.store.get("incidents", run["id"])
                    changes = {"connection_error": None}
                    completed_at = (progress or {}).get("completed_at") or next(
                        (event["completed_at"] for event in reversed(current["timeline"]) if event.get("completed_at")), utcnow(),
                    )
                    if status == "COMPLETED":
                        changes.update(status="completed", stage="completed", completed_at=completed_at)
                    elif status == "CANCELED":
                        changes.update(status="canceled", completed_at=completed_at)
                    elif status in {"FAILED", "TIMED_OUT", "TERMINATED"}:
                        changes.update(status="failed", completed_at=completed_at, error=f"Analysis {status.lower().replace('_', ' ')} during {current['stage']}. Check worker logs and repository access, then rerun.")
                    elif progress and current["status"] == "queued" and progress.get("stage") != "queued":
                        changes.update(status="running", stage=progress["stage"])
                    self.store.update("incidents", run["id"], changes)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.store.update("incidents", run["id"], {
                        "connection_error": "Waiting for Temporal and a worker with matching configuration. Your incident is saved and will resume automatically.",
                    })

    def overview(self) -> Overview:
        rows = self.store.list("incidents")
        durations = [
            (datetime.fromisoformat(r["completed_at"]) - datetime.fromisoformat(r["created_at"])).total_seconds()
            for r in rows if r["status"] == "completed" and r.get("completed_at")
        ]
        dates = [(datetime.now(timezone.utc).date() - timedelta(days=i)).isoformat() for i in reversed(range(14))]
        services = Counter((r.get("report") or {}).get("likely_service") or "Unknown" for r in rows if r["status"] == "completed")
        return Overview(
            total=len(rows), completed=sum(r["status"] == "completed" for r in rows),
            failed=sum(r["status"] == "failed" for r in rows), active=sum(r["status"] not in TERMINAL for r in rows),
            average_duration_seconds=round(sum(durations) / len(durations), 1) if durations else 0,
            activity=[{"date": d, "incidents": sum(r["created_at"][:10] == d for r in rows)} for d in dates],
            services=[{"name": name, "count": count} for name, count in services.most_common()],
            recent=[self.public(row) for row in rows[:6]],
        )
