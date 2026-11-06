from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .config import Configuration, fingerprint
from .models import IncidentRecord, IncidentSubmission, Overview, RepositoryProfile
from .repository_source import public_snapshot_payload, repository_root, source_identity
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
        from temporalio.common import WorkflowIDReusePolicy
        from temporalio.exceptions import WorkflowAlreadyStartedError

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

    async def start_sync(self, job_id: str):
        from temporalio.common import WorkflowIDReusePolicy
        from temporalio.exceptions import WorkflowAlreadyStartedError

        from incident_copilot.workflows.temporal_workflows import SyncRepositoryWorkflow
        client = await self.client()
        try:
            await client.start_workflow(
                SyncRepositoryWorkflow.run, job_id, id=f"repository-sync-{job_id}",
                task_queue=self.configuration.effective().temporal_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                execution_timeout=timedelta(hours=1),
            )
        except WorkflowAlreadyStartedError:
            pass

    async def inspect_sync(self, job_id: str) -> str:
        client = await self.client()
        description = await client.get_workflow_handle(f"repository-sync-{job_id}").describe()
        return description.status.name


class DashboardService:
    def __init__(self, store: ControlStore):
        self.store = store
        self.configuration = Configuration(store)
        from .issues import IssueService
        self.issues = IssueService(store, self.configuration)
        self.gateway = TemporalGateway(self.configuration)
        self.lock = asyncio.Lock()

    def public(self, payload: dict) -> IncidentRecord:
        public = self.configuration.redact(payload)
        if payload.get("repository_source"):
            public = public_snapshot_payload(public, payload["input"]["repo_root"], payload["repository_source"]["remote_url"])
        return IncidentRecord.model_validate(public)

    def public_repository(self, profile: dict) -> RepositoryProfile:
        result = RepositoryProfile.model_validate(profile)
        root = repository_root(profile)
        if profile.get("source_type") == "github" and root and result.index_summary:
            result.index_summary = public_snapshot_payload(result.index_summary, root, profile["remote_url"])
        return result

    def public_sync(self, job: dict) -> dict:
        profile = self.store.get("repositories", job["repository_id"]) or {}
        root = repository_root(profile)
        result = self.configuration.redact(job)
        if root:
            result = public_snapshot_payload(result, root, profile.get("remote_url") or root)
        if result.get("graph_preparation") and profile.get("remote_url"):
            result["graph_preparation"]["repository"] = profile["remote_url"]
        return result

    def sync_status(self, repository_id: str) -> dict:
        profile = self.store.get("repositories", repository_id) or {}
        jobs = [job for job in self.store.list("repository_syncs") if job["repository_id"] == repository_id
                and (job.get("source_identity") == source_identity(profile) or job["status"] in {"queued", "running"})]
        if not jobs:
            return {"repository_id": repository_id, "status": "idle", "stage": "idle"}
        return self.public_sync(max(jobs, key=lambda job: job["created_at"]))

    def request_sync(self, repository_id: str) -> dict:
        now = utcnow()
        job = {"id": str(uuid4()), "repository_id": repository_id, "status": "queued", "stage": "queued",
               "created_at": now, "updated_at": now, "completed_at": None, "result": None,
               "revision": None, "error": None, "connection_error": None, "dispatched": False,
               "config_revision": self.configuration.revision()}
        return self.store.create_repository_sync(job, self.configuration.infrastructure)

    def submit(self, submission: IncidentSubmission, *, original: dict | None = None) -> IncidentRecord:
        profile = self.store.get("repositories", submission.repository_id)
        pinned_rerun = original and original.get("input", {}).get("repository_snapshot")
        if profile is None or (profile.get("archived") and not pinned_rerun):
            raise ValueError("Select an active repository profile.")
        if not submission.log_text.strip():
            raise ValueError("Provide non-empty incident logs.")
        incident_id = str(uuid4())
        config = self.configuration.effective()
        inputs = submission.model_dump() | {
            "repo_root": repository_root(profile), "compose_path": profile.get("compose_path"),
        }
        if original:
            inputs = original["input"] | submission.model_dump()
        elif profile.get("source_type", "local") == "github":
            if not inputs["repo_root"]:
                raise ValueError("Check & sync this GitHub repository before starting an analysis.")
            inputs.update(repository_snapshot=profile["snapshot"], repository_cache_dir=str(config.repository_cache_dir),
                          repository_source={"source_type": "github", "remote_url": profile["remote_url"],
                                             "branch": profile["branch"], "commit": profile["snapshot"]["revision"]})
        # A unique path also prevents stale files after a repository changes.
        index_path = config.index_db_path.parent / "runs" / incident_id / config.index_db_path.name
        record = {
            "id": incident_id, "title": submission.title.strip() or f"Incident {incident_id[:8]}",
            "repository_id": profile["id"], "repository_name": profile["name"],
            "repository_source": inputs.get("repository_source"),
            "status": "queued", "stage": "queued", "created_at": utcnow(), "updated_at": utcnow(),
            "completed_at": None, "config_revision": self.configuration.revision(),
            "index_db_path": str(index_path), "input": inputs, "timeline": [], "report": None,
            "artifact_dir": str(config.data_dir / "incidents" / incident_id),
            "evidence": {"raw_log": submission.log_text}, "error": None, "connection_error": None,
            "dispatched": False,
        }
        capture = None
        if original is None:
            from .issue_capture import prepare_upload

            def project(event):
                event = self.configuration.redact(event)
                if record.get("repository_source"):
                    event = public_snapshot_payload(event, inputs["repo_root"], record["repository_source"]["remote_url"])
                return event

            capture = prepare_upload(submission.log_text, project)
        self.store.create_incident(record, self.configuration.infrastructure, capture=capture,
                                   original_batch_id=original.get("error_batch_id") if original else None)
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
            await self.reconcile_syncs()
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

    async def reconcile_syncs(self):
        for job in self.store.list("repository_syncs"):
            if job["status"] in {"completed", "failed"}:
                continue
            try:
                if not job.get("dispatched"):
                    recent = [w for w in self.store.list("workers") if (datetime.now(timezone.utc) - datetime.fromisoformat(w["updated_at"])).total_seconds() < 20]
                    own = fingerprint(self.configuration.infrastructure)
                    if recent and not any(w["infrastructure"] == own for w in recent):
                        raise RuntimeError("Worker configuration has not been restarted")
                    await asyncio.wait_for(self.gateway.start_sync(job["id"]), timeout=8)
                    self.store.update("repository_syncs", job["id"], {"dispatched": True})
                status = await asyncio.wait_for(self.gateway.inspect_sync(job["id"]), timeout=8)
                current = self.store.get("repository_syncs", job["id"])
                if current["status"] in {"completed", "failed"}:
                    continue
                if status in {"FAILED", "TIMED_OUT", "TERMINATED", "CANCELED", "COMPLETED"}:
                    self.store.update("repository_syncs", job["id"], {
                        "status": "failed", "stage": "failed", "completed_at": utcnow(),
                        "error": "Sync stopped before a snapshot was published. Check the worker and retry sync.", "connection_error": None,
                    })
                else:
                    self.store.update("repository_syncs", job["id"], {"connection_error": None})
            except asyncio.CancelledError:
                raise
            except Exception:
                self.store.update("repository_syncs", job["id"], {
                    "connection_error": "Waiting for Temporal and a worker with matching configuration. This requested sync is saved.",
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
