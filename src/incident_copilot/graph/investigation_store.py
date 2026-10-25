"""Durable reservations and result cache supplement graph step checkpoints.

A process lock serializes each execution, including overlapping activity attempts.
Reservations survive an interrupted node; completed results are reused on resume.
"""
import asyncio
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path


@asynccontextmanager
async def execution_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        acquired = False
        try:
            while not acquired:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError:
                    await asyncio.sleep(0.1)
            yield
        finally:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class InvestigationStore:
    def __init__(self, path: Path):
        self.path = path
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key: str):
        with self.connection() as db:
            row = db.execute("SELECT payload FROM records WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value):
        self.put_many({key: value})

    def put_many(self, values: dict):
        with self.connection() as db:
            db.executemany("INSERT OR REPLACE INTO records VALUES (?,?)",
                           [(key, json.dumps(value, ensure_ascii=False, allow_nan=False))
                            for key, value in values.items()])

    def budget(self) -> dict:
        return self.get("budget")

    def initialize(self, timeout: float):
        if self.get("budget") is None:
            now = time.time()
            self.put("budget", {"started_at": now, "deadline": now + timeout,
                                "rounds": 0, "model_calls": 0, "tool_calls": 0,
                                "content_bytes": 0})

    def reserve(self, field: str, limit: int, *, once_key: str | None = None) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if once_key and db.execute("SELECT 1 FROM records WHERE key=?", (once_key,)).fetchone():
                return True
            budget = json.loads(db.execute("SELECT payload FROM records WHERE key='budget'").fetchone()[0])
            if budget[field] >= limit or time.time() >= budget["deadline"]:
                return False
            budget[field] += 1
            db.execute("UPDATE records SET payload=? WHERE key='budget'", (json.dumps(budget),))
            if once_key:
                db.execute("INSERT INTO records VALUES (?,?)", (once_key, "true"))
        return True

    def set_content_bytes(self, count: int):
        budget = self.budget()
        budget["content_bytes"] = count
        self.put("budget", budget)
