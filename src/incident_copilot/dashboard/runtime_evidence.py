"""Durable collector work. All scope decisions are made outside model arguments."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from incident_copilot.schemas.investigation import EvidenceItem, ToolOutcome
from incident_copilot.schemas.runtime import RuntimeQuery, RuntimeResult, RuntimeScope

SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_collectors (
    collector_id TEXT PRIMARY KEY, updated_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runtime_requests (
    id TEXT PRIMARY KEY, incident_id TEXT NOT NULL REFERENCES incidents(id),
    collector_id TEXT NOT NULL, state TEXT NOT NULL, deadline TEXT NOT NULL,
    lease TEXT, lease_until TEXT, payload TEXT NOT NULL, result TEXT);
CREATE INDEX IF NOT EXISTS runtime_claim ON runtime_requests(collector_id,state,deadline);
CREATE INDEX IF NOT EXISTS runtime_incident ON runtime_requests(incident_id);
CREATE TABLE IF NOT EXISTS runtime_read_reservations (
    incident_id TEXT PRIMARY KEY REFERENCES incidents(id), used INTEGER NOT NULL DEFAULT 0);
"""


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def initialize(db):
    db.executescript(SCHEMA)
    # API and worker may start together against the same control database.
    db.execute("BEGIN IMMEDIATE")
    names = {row[1] for row in db.execute("PRAGMA table_info(collector_sources)")}
    for name, declaration in {
        "runtime_enabled": "INTEGER NOT NULL DEFAULT 1",
        "runtime_dependencies": "TEXT NOT NULL DEFAULT '{}'",
        "telemetry_profile": "TEXT",
    }.items():
        if name not in names:
            db.execute(f"ALTER TABLE collector_sources ADD COLUMN {name} {declaration}")


def register(db, collector_id, capabilities, *, helper=None):
    now = datetime.now(UTC).isoformat()
    payload = {"version": 1, "capabilities": sorted(set(capabilities)), "helper": helper or {}}
    db.execute(
        "INSERT OR REPLACE INTO runtime_collectors VALUES (?,?,?)",
        (collector_id, now, encoded(payload)),
    )


def capture_scope(db, record, capture):
    deployment = record.get("deployment_context")
    if not deployment:
        return None
    source = db.execute(
        "SELECT * FROM collector_sources WHERE collector_id=? AND project=?",
        (deployment["collector_id"], deployment["project"]),
    ).fetchone()
    host = db.execute(
        "SELECT payload FROM runtime_collectors WHERE collector_id=?", (deployment["collector_id"],)
    ).fetchone()
    if not source or not source["enabled"] or not source["runtime_enabled"] or not host:
        return None
    services = [deployment["service"]]
    services += sorted(
        set(json.loads(source["runtime_dependencies"]).get(services[0], [])) - set(services)
    )
    limits = ["Related service scope clipped."] if len(services) > 5 else []
    from incident_copilot.correlation.log_parser import parse_logs

    # Reparse the actual submitted text: collector batches may submit only one sliced event.
    parsed = parse_logs(record["input"]["log_text"])
    events = parsed.incident_context.events if parsed.incident_context else []
    errors = [event for event in events if event.error_bearing]
    matching = [event for event in errors if event.service == deployment["service"]]
    selected = next(iter(matching or errors), None)
    event = selected.model_dump() if selected else {}
    stamp = event.get("timestamp_utc")
    if not stamp:
        limits.append("No usable event timestamp; using ingestion time.")
    containers = {}
    rows = db.execute(
        "SELECT payload FROM deployments WHERE collector_id=? AND project=? ORDER BY json_extract(payload,'$.last_observed') DESC LIMIT 200",
        (deployment["collector_id"], deployment["project"]),
    ).fetchall()
    for row in rows:
        item = json.loads(row[0])
        containers.setdefault(item["service"], []).append(item["container_id"])
    # The failing container is always pinned, including a stopped container absent from inventory.
    containers[deployment["service"]] = [deployment["container_id"]]
    return RuntimeScope(
        collector_id=deployment["collector_id"],
        project=deployment["project"],
        environment=deployment["environment"],
        service=deployment["service"],
        container_id=deployment["container_id"],
        services=services[:5],
        containers=containers,
        anchor=stamp or record["created_at"],
        trace_ids=[event["trace_id"]] if event.get("trace_id") else [],
        request_ids=[event["request_id"]] if event.get("request_id") else [],
        trigger_event_id=event.get("id"),
        telemetry_profile=source["telemetry_profile"],
        limitations=limits,
        policy_version=hashlib.sha256(
            encoded(
                {
                    key: source[key]
                    for key in (
                        "runtime_dependencies",
                        "telemetry_profile",
                        "repository_id",
                        "environment",
                    )
                }
            ).encode()
        ).hexdigest()[:24],
    ).model_dump(mode="json")


def enqueue(db, record, query, *, baseline=False):
    scope = record["runtime_scope"]
    key = hashlib.sha256(encoded([record["id"], scope, query, baseline]).encode()).hexdigest()
    deadline = datetime.now(UTC) + timedelta(seconds=30 if baseline else 15)
    if baseline:
        deadline = datetime.fromisoformat(record["created_at"]) + timedelta(seconds=30)
    payload = {"scope": scope, "query": query, "baseline": baseline}
    db.execute(
        "INSERT OR IGNORE INTO runtime_requests(id,incident_id,collector_id,state,deadline,payload) VALUES (?,?,?,'queued',?,?)",
        (key, record["id"], scope["collector_id"], deadline.isoformat(), encoded(payload)),
    )
    return key


def create_baseline(db, record, capture):
    if record.pop("runtime_skip", False):
        return
    # Reruns retain observations and the original scope; they do not resample live state.
    if "runtime_seed" in record:
        for item in record.pop("runtime_seed"):
            db.execute(
                "INSERT OR IGNORE INTO runtime_requests(id,incident_id,collector_id,state,deadline,payload,result) VALUES (?,?,?,'completed',?,?,?)",
                (
                    str(uuid4()),
                    record["id"],
                    record["runtime_scope"]["collector_id"],
                    record["created_at"],
                    item["payload"],
                    item["result"],
                ),
            )
        return
    scope = capture_scope(db, record, capture)
    if scope:
        record["runtime_scope"] = scope
        for capability in ["traces", "dependencies", "resources", "logs", "latency"]:
            enqueue(
                db,
                record,
                RuntimeQuery(capability=capability).model_dump(mode="json"),
                baseline=True,
            )


def enabled(db, record):
    scope = record.get("runtime_scope") or {}
    row = db.execute(
        "SELECT enabled,runtime_enabled,repository_id FROM collector_sources WHERE collector_id=? AND project=?",
        (scope.get("collector_id"), scope.get("project")),
    ).fetchone()
    return bool(
        row
        and row["enabled"]
        and row["runtime_enabled"]
        and row["repository_id"] == record["repository_id"]
        and record["status"] in {"queued", "running"}
        and not record.get("runtime_canceled")
    )


def effective_scope(db, record):
    scope = RuntimeScope.model_validate(record["runtime_scope"])
    for row in db.execute(
        "SELECT result FROM runtime_requests WHERE incident_id=? AND state='completed' AND result IS NOT NULL",
        (record["id"],),
    ):
        result = json.loads(row[0])
        if result["capability"] == "traces":
            for span in result.get("data", {}).get("spans", []):
                service = span.get("service")
                if (
                    service in scope.containers
                    and service not in scope.services
                    and len(scope.services) < 5
                ):
                    scope.services.append(service)
    return scope


def validate_query(query, scope):
    if query.service and query.service not in scope.services:
        raise ValueError("service_outside_runtime_scope")
    if query.trace_id and query.trace_id not in scope.trace_ids:
        raise ValueError("unobserved_trace_id")


def expire(db):
    now = datetime.now(UTC).isoformat()
    db.execute(
        "UPDATE runtime_requests SET state='expired',lease=NULL WHERE state IN ('queued','leased') AND deadline<=?",
        (now,),
    )
    db.execute(
        "UPDATE runtime_requests SET state='queued',lease=NULL WHERE state='leased' AND lease_until<=? AND deadline>?",
        (now, now),
    )


def claim(store, collector_id, capabilities, helper):
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        register(db, collector_id, capabilities, helper=helper)
        expire(db)
        active = db.execute(
            "SELECT COUNT(*) FROM runtime_requests WHERE collector_id=? AND state='leased'",
            (collector_id,),
        ).fetchone()[0]
        jobs = []
        for row in db.execute(
            "SELECT * FROM runtime_requests WHERE collector_id=? AND state='queued' ORDER BY deadline, CASE json_extract(payload,'$.query.capability') WHEN 'traces' THEN 0 ELSE 1 END,id LIMIT 100",
            (collector_id,),
        ).fetchall():
            record_row = db.execute(
                "SELECT payload FROM incidents WHERE id=?", (row["incident_id"],)
            ).fetchone()
            record = json.loads(record_row[0]) if record_row else {}
            if not enabled(db, record):
                db.execute(
                    "UPDATE runtime_requests SET state='canceled',lease=NULL WHERE id=?",
                    (row["id"],),
                )
                continue
            if active + len(jobs) >= 2:
                break
            payload = json.loads(row["payload"])
            scope = effective_scope(db, record)
            query = RuntimeQuery.model_validate(payload["query"])
            validate_query(query, scope)
            max_reads = 30
            if payload["baseline"]:
                reserved = db.execute(
                    "SELECT used FROM runtime_read_reservations WHERE incident_id=?",
                    (record["id"],),
                ).fetchone()
                available = 30 - (reserved[0] if reserved else 0)
                quota = {"traces": 2, "dependencies": 8, "resources": 5, "logs": 10, "latency": 5}[
                    query.capability
                ]
                max_reads = min(quota, available)
                if max_reads <= 0:
                    db.execute(
                        "UPDATE runtime_requests SET state='budget_exhausted',lease=NULL WHERE id=?",
                        (row["id"],),
                    )
                    continue
                # Charge before dispatch; even a lost acknowledgement or helper restart cannot reset reads.
                db.execute(
                    "INSERT INTO runtime_read_reservations VALUES (?,?) ON CONFLICT(incident_id) DO UPDATE SET used=used+excluded.used",
                    (record["id"], max_reads),
                )
            lease = str(uuid4())
            until = min(
                datetime.fromisoformat(row["deadline"]), datetime.now(UTC) + timedelta(seconds=15)
            ).isoformat()
            db.execute(
                "UPDATE runtime_requests SET state='leased',lease=?,lease_until=? WHERE id=?",
                (lease, until, row["id"]),
            )
            jobs.append(
                {
                    "id": row["id"],
                    "incident_id": row["incident_id"],
                    "baseline": payload["baseline"],
                    "lease": lease,
                    "deadline": row["deadline"],
                    "max_reads": max_reads,
                    "scope": scope.model_dump(mode="json"),
                    "query": payload["query"],
                }
            )
        policies = [
            dict(row)
            for row in db.execute(
                "SELECT project,environment,services FROM collector_sources WHERE collector_id=? AND enabled=1 AND runtime_enabled=1",
                (collector_id,),
            )
        ]
    return {"jobs": jobs, "policies": policies}


def accept(store, collector_id, request_id, lease, result, redact):
    result = RuntimeResult.model_validate(result)
    if len(encoded(result.model_dump(mode="json")).encode()) > 8192:
        raise ValueError("runtime_result_limit")
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM runtime_requests WHERE id=? AND collector_id=?",
            (request_id, collector_id),
        ).fetchone()
        if not row:
            raise ValueError("unknown_runtime_request")
        if row["state"] == "completed" and row["lease"] == lease:
            return {"accepted": False, "reason": "duplicate"}
        record = json.loads(
            db.execute(
                "SELECT payload FROM incidents WHERE id=?", (row["incident_id"],)
            ).fetchone()[0]
        )
        if (
            row["state"] != "leased"
            or row["lease"] != lease
            or min(row["deadline"], row["lease_until"]) <= datetime.now(UTC).isoformat()
            or not enabled(db, record)
        ):
            raise ValueError("runtime_lease_inactive")
        payload = json.loads(row["payload"])
        scope = effective_scope(db, record)
        query = RuntimeQuery.model_validate(payload["query"])
        provenance = result.provenance
        if (
            result.capability != query.capability
            or provenance.collector_id != scope.collector_id
            or provenance.project != scope.project
            or provenance.environment != scope.environment
            or provenance.service != (query.service or scope.service)
            or provenance.requested_end != scope.anchor
            or provenance.requested_start
            != scope.anchor - timedelta(minutes=query.lookback_minutes)
        ):
            raise ValueError("runtime_result_scope_mismatch")
        allowed_services = set(scope.services)
        if query.capability == "traces":
            for span in result.data.get("spans", []):
                if (
                    span.get("trace_id") not in scope.trace_ids
                    or span.get("service") not in scope.containers
                ):
                    raise ValueError("runtime_result_scope_mismatch")
                if len(allowed_services) < 5:
                    allowed_services.add(span["service"])
        allowed_ids = {
            container for name in allowed_services for container in scope.containers.get(name, [])
        }
        if not set(provenance.container_ids) <= allowed_ids:
            raise ValueError("runtime_result_scope_mismatch")

        def check(value):
            if isinstance(value, dict):
                if "service" in value and value["service"] not in allowed_services:
                    raise ValueError("runtime_result_scope_mismatch")
                if "container_id" in value and value["container_id"] not in allowed_ids:
                    raise ValueError("runtime_result_scope_mismatch")
                if value.get("trace_id") and value["trace_id"] not in scope.trace_ids:
                    raise ValueError("runtime_result_scope_mismatch")
                if (
                    value.get("request_id")
                    and value["request_id"] not in scope.request_ids
                    and value.get("trace_id") not in scope.trace_ids
                ):
                    raise ValueError("runtime_result_scope_mismatch")
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)

        check(result.data)
        from incident_copilot.analysis.evidence_tools import redact_evidence
        from incident_copilot.core.settings import Settings

        safe = redact_evidence(redact(result.model_dump(mode="json")), Settings())
        db.execute(
            "UPDATE runtime_requests SET state='completed',result=? WHERE id=?",
            (encoded(safe), request_id),
        )
    return {"accepted": True}


def snapshot(store, incident_id):
    with store.connection() as db:
        expire(db)
        db.execute(
            "UPDATE runtime_requests SET state='canceled',lease=NULL WHERE incident_id=? AND state IN ('queued','leased') AND EXISTS (SELECT 1 FROM incidents WHERE id=? AND status NOT IN ('queued','running'))",
            (incident_id, incident_id),
        )
        rows = db.execute(
            "SELECT * FROM runtime_requests WHERE incident_id=? ORDER BY id", (incident_id,)
        ).fetchall()
    evidence, collection, size = [], [], 0
    for row in rows:
        query = json.loads(row["payload"])["query"]
        result = json.loads(row["result"]) if row["result"] else None
        collection.append(
            {
                "id": row["id"],
                "capability": query["capability"],
                "status": result["status"] if result else row["state"],
                "reason": result.get("reason") if result else None,
                "duration_ms": result.get("duration_ms", 0) if result else 0,
            }
        )
        if result:
            item = EvidenceItem(
                id="rt_" + row["id"][:24],
                kind="runtime_" + result["capability"],
                runtime=result["provenance"],
                data=result["data"] | {"status": result["status"], "reason": result["reason"]},
                limitations=result["limitations"],
                truncated=result["truncated"],
                request_id=row["id"],
            ).model_dump(mode="json")
            item_size = len(encoded(item).encode())
            if size + item_size <= 65536:
                evidence.append(item)
                size += item_size
    return {"runtime_collection": collection, "runtime_evidence": evidence}


class RuntimeEvidenceClient:
    def __init__(self, store, incident_id):
        self.store, self.incident_id = store, incident_id

    def scope(self):
        record = self.store.get("incidents", self.incident_id)
        with self.store.connection() as db:
            return effective_scope(db, record).model_dump(mode="json")

    async def baseline(self):
        while True:
            result = await asyncio.to_thread(snapshot, self.store, self.incident_id)
            if not any(
                item["status"] in {"queued", "leased"} for item in result["runtime_collection"]
            ):
                return result
            await asyncio.sleep(0.25)

    async def execute(self, request, request_id):
        query = RuntimeQuery(
            capability=request.capability,
            service=request.service,
            trace_id=request.trace_id,
            lookback_minutes=request.lookback_minutes,
        )
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            record = json.loads(
                db.execute(
                    "SELECT payload FROM incidents WHERE id=?", (self.incident_id,)
                ).fetchone()[0]
            )
            scope = effective_scope(db, record)
            validate_query(query, scope)
            if not enabled(db, record):
                return ToolOutcome(
                    request_id=request_id,
                    tool=request.tool,
                    status="unavailable",
                    reason="runtime_disabled",
                )
            count = db.execute(
                "SELECT COUNT(*) FROM runtime_requests WHERE incident_id=? AND json_extract(payload,'$.baseline')=0",
                (self.incident_id,),
            ).fetchone()[0]
            key = hashlib.sha256(
                encoded(
                    [record["id"], record["runtime_scope"], query.model_dump(mode="json"), False]
                ).encode()
            ).hexdigest()
            existing = db.execute("SELECT 1 FROM runtime_requests WHERE id=?", (key,)).fetchone()
            if count >= 4 and not existing:
                return ToolOutcome(
                    request_id=request_id,
                    tool=request.tool,
                    status="unavailable",
                    reason="runtime_request_limit",
                )
            key = enqueue(db, record, query.model_dump(mode="json"))
        try:
            while True:
                view = await asyncio.to_thread(snapshot, self.store, self.incident_id)
                state = next(item for item in view["runtime_collection"] if item["id"] == key)
                if state["status"] not in {"queued", "leased"}:
                    items = [
                        EvidenceItem.model_validate(item)
                        for item in view["runtime_evidence"]
                        if item["request_id"] == key
                    ]
                    return ToolOutcome(
                        request_id=request_id,
                        tool=request.tool,
                        status="ok"
                        if items and state["status"] in {"ok", "partial"}
                        else "empty"
                        if state["status"] == "empty"
                        else "unavailable",
                        reason=state.get("reason") or state["status"],
                        evidence=items,
                    )
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            with self.store.connection() as db:
                db.execute(
                    "UPDATE runtime_requests SET state='canceled' WHERE id=? AND state IN ('queued','leased')",
                    (key,),
                )
            raise
