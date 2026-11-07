from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


class ControlStore:
    """Small shared SQLite store. Each operation owns its connection and transaction."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS repositories (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL,
                    repository_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS incident_status ON incidents(status);
                CREATE INDEX IF NOT EXISTS incident_created ON incidents(created_at DESC);
                CREATE TABLE IF NOT EXISTS configuration (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS workers (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS repository_credentials (id TEXT PRIMARY KEY, token TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS repository_syncs (
                    id TEXT PRIMARY KEY, repository_id TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS repository_active_sync
                    ON repository_syncs(repository_id) WHERE status IN ('queued','running');
                CREATE TABLE IF NOT EXISTS log_sources (
                    project TEXT PRIMARY KEY, services TEXT NOT NULL DEFAULT '[]',
                    repository_id TEXT, enabled INTEGER NOT NULL DEFAULT 0,
                    last_seen TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS log_ingest_events (
                    event_id TEXT PRIMARY KEY, project TEXT NOT NULL, received_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS log_investigation_cooldowns (
                    repository_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    last_started TEXT NOT NULL, PRIMARY KEY(repository_id, fingerprint));
            """)
            from .issue_capture import SCHEMA
            db.executescript(SCHEMA)
            from .deployments import SCHEMA as DEPLOYMENT_SCHEMA
            db.executescript(DEPLOYMENT_SCHEMA)
            from .runtime_evidence import initialize as initialize_runtime
            initialize_runtime(db)
            # Additive, idempotent migration; the original table remains intact.
            db.execute("""INSERT OR IGNORE INTO collector_sources
                (collector_id,project,services,repository_id,enabled,last_seen)
                SELECT 'legacy',project,services,repository_id,enabled,last_seen FROM log_sources""")
            db.execute("INSERT OR IGNORE INTO configuration(key,payload) VALUES ('error_tracking',?)",
                       (json.dumps({"schema_version": 1, "tracking_since": utcnow()}),))

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, table: str, key: str) -> dict | None:
        self._table(table)
        with self.connection() as db:
            row = db.execute(f"SELECT payload FROM {table} WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, table: str, *, exclude_id_prefix: str | None = None) -> list[dict]:
        self._table(table)
        order = " ORDER BY created_at DESC, id DESC" if table == "incidents" else ""
        with self.connection() as db:
            if exclude_id_prefix is None:
                rows = db.execute(f"SELECT payload FROM {table}{order}").fetchall()
            else:
                rows = db.execute(
                    f"SELECT payload FROM {table} WHERE substr(id, 1, ?) != ?{order}",
                    (len(exclude_id_prefix), exclude_id_prefix),
                ).fetchall()
        return [json.loads(row[0]) for row in rows]

    @staticmethod
    def _table(table: str) -> None:
        if table not in {"repositories", "incidents", "revisions", "workers", "repository_syncs"}:
            raise ValueError("Unknown control-store table")

    def put(self, table: str, key: str, payload: dict) -> None:
        self._table(table)
        with self.connection() as db:
            self._put(db, table, key, payload)

    @staticmethod
    def _put(db: sqlite3.Connection, table: str, key: str, payload: dict) -> None:
        if table == "incidents":
            db.execute(
                "INSERT INTO incidents VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "status=excluded.status, payload=excluded.payload",
                (key, payload["status"], payload["created_at"], payload["repository_id"], json.dumps(payload)),
            )
        elif table == "repository_syncs":
            db.execute(
                "INSERT INTO repository_syncs VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "status=excluded.status, payload=excluded.payload",
                (key, payload["repository_id"], payload["status"], payload["created_at"], json.dumps(payload)),
            )
        else:
            db.execute(f"INSERT OR REPLACE INTO {table}(id,payload) VALUES (?,?)", (key, json.dumps(payload)))

    def repository_token(self, key: str) -> str:
        with self.connection() as db:
            row = db.execute("SELECT token FROM repository_credentials WHERE id=?", (key,)).fetchone()
        return row[0] if row else ""

    def repository_secrets(self) -> set[str]:
        with self.connection() as db:
            return {row[0] for row in db.execute("SELECT token FROM repository_credentials") if row[0]}

    def save_repository(self, key: str, values: dict, *, token: str = "", clear_token: bool = False) -> dict:
        from .repository_source import source_identity
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM repositories WHERE id=?", (key,)).fetchone()
            previous = json.loads(row[0]) if row else {}
            profile = previous | values | {"id": key}
            profile.setdefault("created_at", utcnow())
            profile.setdefault("archived", False)
            if source_identity(previous) != source_identity(profile):
                profile.update(snapshot=None, synced_revision=None, last_synced_at=None,
                               last_checked_at=None, validation=None, index_summary=None)
            if clear_token or profile.get("source_type", "local") == "local":
                db.execute("DELETE FROM repository_credentials WHERE id=?", (key,))
            elif token:
                db.execute("INSERT OR REPLACE INTO repository_credentials VALUES (?,?)", (key, token))
            profile["token_configured"] = db.execute("SELECT 1 FROM repository_credentials WHERE id=?", (key,)).fetchone() is not None
            self._put(db, "repositories", key, profile)
            return profile

    def create_repository_sync(self, job: dict, infrastructure: dict) -> dict:
        from .repository_source import source_identity
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM configuration WHERE key='infrastructure'").fetchone()
            if row and json.loads(row[0]) != infrastructure:
                raise ValueError("Restart both API and worker before syncing with changed infrastructure settings.")
            row = db.execute("SELECT payload FROM repositories WHERE id=?", (job["repository_id"],)).fetchone()
            profile = json.loads(row[0]) if row else None
            if not profile or profile.get("archived") or profile.get("source_type", "local") != "github":
                raise ValueError("Select an active GitHub repository to sync.")
            row = db.execute("SELECT payload FROM repository_syncs WHERE repository_id=? AND status IN ('queued','running')", (profile["id"],)).fetchone()
            if row:
                return json.loads(row[0])
            job["source"] = {key: profile.get(key) for key in ("source_type", "repo_root", "remote_url", "branch", "compose_path")}
            job["source_identity"] = source_identity(profile)
            self._put(db, "repository_syncs", job["id"], job)
            return job

    def publish_repository_sync(self, job_id: str, snapshot: dict, *, unchanged: bool, graph: dict | None) -> None:
        from .repository_source import source_identity
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM repository_syncs WHERE id=?", (job_id,)).fetchone()
            job = json.loads(row[0])
            row = db.execute("SELECT payload FROM repositories WHERE id=?", (job["repository_id"],)).fetchone()
            profile = json.loads(row[0]) if row else None
            if not profile or profile.get("archived") or source_identity(profile) != job["source_identity"]:
                raise ValueError("Repository configuration changed during sync. Check and sync the current profile again.")
            now = utcnow()
            profile.update(snapshot=snapshot, synced_revision=snapshot["revision"], last_checked_at=job.get("checked_at") or now,
                           validation=snapshot["validation"], index_summary=snapshot["summary"])
            if not unchanged:
                profile["last_synced_at"] = now
            self._put(db, "repositories", profile["id"], profile)
            job.update(status="completed", stage="completed", result="unchanged" if unchanged else "updated",
                       revision=snapshot["revision"], graph_preparation=graph,
                       updated_at=now, completed_at=now, error=None, connection_error=None)
            self._put(db, "repository_syncs", job_id, job)

    def record_repository_check(self, job_id: str, revision: str) -> None:
        from .repository_source import source_identity
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            job = json.loads(db.execute("SELECT payload FROM repository_syncs WHERE id=?", (job_id,)).fetchone()[0])
            job.update(checked_at=utcnow(), revision=revision)
            self._put(db, "repository_syncs", job_id, job)
            row = db.execute("SELECT payload FROM repositories WHERE id=?", (job["repository_id"],)).fetchone()
            profile = json.loads(row[0]) if row else None
            if profile and not profile.get("archived") and source_identity(profile) == job["source_identity"]:
                profile["last_checked_at"] = job["checked_at"]
                self._put(db, "repositories", profile["id"], profile)

    def update(self, table: str, key: str, changes: dict) -> dict:
        self._table(table)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(f"SELECT payload FROM {table} WHERE id=?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            payload = json.loads(row[0])
            payload.update(changes)
            self._put(db, table, key, payload)
        return payload

    def create_incident(self, record: dict, infrastructure: dict, *, capture: dict | None = None,
                        original_batch_id: str | None = None, collector_event: dict | None = None) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM configuration WHERE key='infrastructure'").fetchone()
            if row and json.loads(row[0]) != infrastructure:
                raise ValueError("Infrastructure settings changed. Restart both the API and worker before submitting a new incident.")
            if collector_event is not None:
                from .issue_capture import capture_upload
                key = collector_event["event_id"]
                if db.execute("SELECT 1 FROM log_ingest_events WHERE event_id=?", (key,)).fetchone():
                    collector_event["outcome"] = (0, 0)
                    return False
                now = record["created_at"]
                db.execute("INSERT INTO log_ingest_events VALUES (?,?,?)", (key, collector_event["project"], now))
                cooldown_key = collector_event["cooldown_key"]
                previous = db.execute("SELECT last_started FROM log_investigation_cooldowns WHERE repository_id=? AND fingerprint=?",
                                      (record["repository_id"], cooldown_key)).fetchone()
                if previous and datetime.fromisoformat(previous[0]) > datetime.now(UTC) - timedelta(hours=1):
                    capture_upload(db, record, capture, link_incident=False)
                    collector_event["outcome"] = (1, 0)
                    return False
                db.execute("""INSERT INTO log_investigation_cooldowns VALUES (?,?,?)
                    ON CONFLICT(repository_id,fingerprint) DO UPDATE SET last_started=excluded.last_started""",
                    (record["repository_id"], cooldown_key, now))
                collector_event["outcome"] = (1, 1)
            self._put(db, "incidents", record["id"], record)
            if capture is not None:
                from .issue_capture import capture_upload
                capture_upload(db, record, capture)
            elif original_batch_id:
                db.execute("INSERT INTO incident_error_batches VALUES (?,?)", (record["id"], original_batch_id))
                record.update(error_batch_id=original_batch_id, error_tracking_status="duplicate")
            from .runtime_evidence import create_baseline
            create_baseline(db, record, capture)
            self._put(db, "incidents", record["id"], record)
        return True

    def config(self, key: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM configuration WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set_config(self, key: str, value: dict) -> None:
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO configuration VALUES (?,?)", (key, json.dumps(value)))

    def activate_infrastructure(self, desired: dict[str, Any]) -> dict[str, Any]:
        # The activation and active-run check must share one write transaction.
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM configuration WHERE key='infrastructure'").fetchone()
            current = json.loads(row[0]) if row else {}
            busy = db.execute("SELECT 1 FROM incidents WHERE status IN ('queued','running') LIMIT 1").fetchone()
            busy = busy or db.execute("SELECT 1 FROM repository_syncs WHERE status IN ('queued','running') LIMIT 1").fetchone()
            if not busy or not current:
                current = desired
                db.execute("INSERT OR REPLACE INTO configuration VALUES ('infrastructure',?)", (json.dumps(current),))
            return current
