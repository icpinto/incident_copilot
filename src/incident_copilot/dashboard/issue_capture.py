"""Deterministic, transactional error capture. No repository or model access."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from uuid import uuid4

from incident_copilot.correlation.log_parser import parse_logs

UNKNOWN = "(unknown)"
SCHEMA = """
CREATE TABLE IF NOT EXISTS error_batches (
    id TEXT PRIMARY KEY, repository_id TEXT NOT NULL, content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL, parser_version INTEGER NOT NULL,
    UNIQUE(repository_id, content_hash));
CREATE TABLE IF NOT EXISTS incident_error_batches (
    incident_id TEXT PRIMARY KEY REFERENCES incidents(id),
    batch_id TEXT NOT NULL REFERENCES error_batches(id));
CREATE INDEX IF NOT EXISTS error_batch_incidents ON incident_error_batches(batch_id);
CREATE TABLE IF NOT EXISTS error_issues (
    id TEXT PRIMARY KEY, repository_id TEXT NOT NULL, title TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open'
        CHECK(status IN ('open','acknowledged','investigating','resolved','ignored')),
    resolution_note TEXT NOT NULL DEFAULT '', resolved_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, merged_into TEXT REFERENCES error_issues(id));
CREATE INDEX IF NOT EXISTS error_issue_triage ON error_issues(repository_id, status, owner);
CREATE TABLE IF NOT EXISTS error_fingerprints (
    repository_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
    issue_id TEXT NOT NULL REFERENCES error_issues(id), PRIMARY KEY(repository_id, fingerprint));
CREATE TABLE IF NOT EXISTS error_occurrences (
    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES error_batches(id),
    issue_id TEXT NOT NULL REFERENCES error_issues(id), event_id TEXT NOT NULL,
    effective_at TEXT NOT NULL, source_timestamp INTEGER NOT NULL CHECK(source_timestamp IN (0,1)),
    payload TEXT NOT NULL, UNIQUE(batch_id, event_id));
CREATE INDEX IF NOT EXISTS error_occurrence_time ON error_occurrences(effective_at, issue_id);
CREATE INDEX IF NOT EXISTS error_occurrence_issue ON error_occurrences(issue_id, effective_at, id);
CREATE INDEX IF NOT EXISTS error_occurrence_batch ON error_occurrences(batch_id);
CREATE TABLE IF NOT EXISTS error_dimensions (
    occurrence_id TEXT NOT NULL REFERENCES error_occurrences(id), kind TEXT NOT NULL,
    value TEXT NOT NULL, PRIMARY KEY(occurrence_id, kind, value));
CREATE INDEX IF NOT EXISTS error_dimension_lookup ON error_dimensions(kind, value, occurrence_id);
CREATE TABLE IF NOT EXISTS error_issue_activity (
    id TEXT PRIMARY KEY, issue_id TEXT NOT NULL REFERENCES error_issues(id),
    kind TEXT NOT NULL, created_at TEXT NOT NULL, effective_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS error_activity_issue ON error_issue_activity(issue_id, created_at, id);
CREATE INDEX IF NOT EXISTS error_activity_time ON error_issue_activity(kind, effective_at, issue_id);
"""


def normalize_message(value: str) -> str:
    value = re.sub(
        r"\b\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?", "<time>", value
    )
    value = re.sub(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b", "<uuid>", value)
    value = re.sub(
        r"\b(trace_id|traceId|request_id|requestId)\s*[=:]\s*[\"']?[\w.:-]+[\"']?",
        r"\1=<id>",
        value,
    )
    value = re.sub(r"(?<![\w])[-+]?\d+(?:\.\d+)?(?![\w])", "<number>", value)
    return " ".join(value.split())


def event_fingerprint(event: dict) -> str:
    exceptions = event["exceptions"]
    signature = {
        "version": 1,
        "service": event.get("service"),
        "exceptions": [(item["type"], normalize_message(item["message"])) for item in exceptions],
        "message": None if exceptions else normalize_message(event["message"]),
        "frames": [
            (
                frame.get("file_path", "").replace("\\", "/") if frame.get("file_path") else None,
                frame.get("function_name"),
            )
            for frame in event["stack_frames"]
        ],
    }
    return "v1:" + hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()


def prepare_upload(log_text: str, project) -> dict:
    context = parse_logs(log_text).incident_context
    group_for_event = {
        event_id: group.id for group in context.groups for event_id in group.event_ids
    }
    # Prepare credential/path redaction once per upload, rather than querying
    # configuration and historical credentials once for every error record.
    projected = project(
        {
            "events": [
                event.model_dump(mode="json") for event in context.events if event.error_bearing
            ]
        }
    )
    events = []
    for event in projected["events"]:
        event["group_id"] = group_for_event.get(event["id"])
        if not event.get("timestamp_utc"):
            event["limitations"].append(
                "Analytics uses original submission time because no usable timezone-aware event timestamp is available."
            )
        events.append({"event": event, "fingerprint": event_fingerprint(event)})
    return {"content_hash": hashlib.sha256(log_text.encode("utf-8")).hexdigest(), "events": events}


def activity(
    db: sqlite3.Connection,
    issue_id: str,
    kind: str,
    now: str,
    details: dict,
    effective_at: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO error_issue_activity VALUES (?,?,?,?,?,?)",
        (str(uuid4()), issue_id, kind, now, effective_at or now, json.dumps(details)),
    )


def new_issue(
    db: sqlite3.Connection, repository_id: str, title: str, now: str, owner: str = ""
) -> str:
    issue_id = str(uuid4())
    db.execute(
        "INSERT INTO error_issues(id,repository_id,title,owner,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        (issue_id, repository_id, title[:200], owner, now, now),
    )
    activity(db, issue_id, "created", now, {})
    return issue_id


def capture_upload(db: sqlite3.Connection, record: dict, capture: dict, *, link_incident: bool = True) -> None:
    repository_id, now = record["repository_id"], record["created_at"]
    batch = db.execute(
        "SELECT id FROM error_batches WHERE repository_id=? AND content_hash=?",
        (repository_id, capture["content_hash"]),
    ).fetchone()
    record["error_tracking_status"] = "duplicate" if batch else "tracked"
    batch_id = batch["id"] if batch else str(uuid4())
    record["error_batch_id"] = batch_id
    if not batch:
        db.execute(
            "INSERT INTO error_batches VALUES (?,?,?,?,?)",
            (batch_id, repository_id, capture["content_hash"], now, 1),
        )
        for item in capture["events"]:
            event = item["event"]
            route = db.execute(
                "SELECT issue_id FROM error_fingerprints WHERE repository_id=? AND fingerprint=?",
                (repository_id, item["fingerprint"]),
            ).fetchone()
            if route:
                issue_id = route["issue_id"]
            else:
                title = next(
                    (f"{e['type']}: {e['message']}" for e in event["exceptions"]), event["message"]
                )
                issue_id = new_issue(db, repository_id, title, now)
                db.execute(
                    "INSERT INTO error_fingerprints VALUES (?,?,?)",
                    (repository_id, item["fingerprint"], issue_id),
                )
            effective_at = event.get("timestamp_utc") or now
            current = db.execute(
                "SELECT status,resolved_at FROM error_issues WHERE id=?", (issue_id,)
            ).fetchone()
            if current["status"] == "resolved" and effective_at > current["resolved_at"]:
                db.execute(
                    "UPDATE error_issues SET status='open',resolved_at=NULL WHERE id=?", (issue_id,)
                )
                activity(
                    db,
                    issue_id,
                    "reopened",
                    now,
                    {
                        "reason": "New occurrence after resolution",
                        "previous_resolved_at": current["resolved_at"],
                    },
                    effective_at,
                )
            db.execute("UPDATE error_issues SET updated_at=? WHERE id=?", (now, issue_id))
            occurrence_id = str(uuid4())
            db.execute(
                "INSERT INTO error_occurrences VALUES (?,?,?,?,?,?,?)",
                (
                    occurrence_id,
                    batch_id,
                    issue_id,
                    event["id"],
                    effective_at,
                    int(bool(event.get("timestamp_utc"))),
                    json.dumps(event),
                ),
            )
            dimensions = {
                "repository": {repository_id},
                "service": {event.get("service") or UNKNOWN},
                "error_class": {e["type"] for e in event["exceptions"]} or {UNKNOWN},
                "file": {
                    f["file_path"].replace("\\", "/")
                    for f in event["stack_frames"]
                    if f.get("file_path")
                }
                or {UNKNOWN},
                "function": {
                    f["function_name"] for f in event["stack_frames"] if f.get("function_name")
                }
                or {UNKNOWN},
            }
            db.executemany(
                "INSERT INTO error_dimensions VALUES (?,?,?)",
                [
                    (occurrence_id, kind, value)
                    for kind, values in dimensions.items()
                    for value in values
                ],
            )
    if link_incident:
        db.execute("INSERT INTO incident_error_batches VALUES (?,?)", (record["id"], batch_id))
