from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
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
            """)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
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
        if table not in {"repositories", "incidents", "revisions", "workers"}:
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
        else:
            db.execute(f"INSERT OR REPLACE INTO {table}(id,payload) VALUES (?,?)", (key, json.dumps(payload)))

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

    def create_incident(self, record: dict, infrastructure: dict) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM configuration WHERE key='infrastructure'").fetchone()
            if row and json.loads(row[0]) != infrastructure:
                raise ValueError("Infrastructure settings changed. Restart both the API and worker before submitting a new incident.")
            self._put(db, "incidents", record["id"], record)

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
            if not busy or not current:
                current = desired
                db.execute("INSERT OR REPLACE INTO configuration VALUES ('infrastructure',?)", (json.dumps(current),))
            return current
