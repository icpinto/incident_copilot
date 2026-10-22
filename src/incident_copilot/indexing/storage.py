import json
import sqlite3
from pathlib import Path
from typing import Iterable, List

from incident_copilot.schemas.index import FileRecord, RepoIndexSummary, ServiceMapping


class IndexStorage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS services (
                  service_name TEXT PRIMARY KEY,
                  payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                  file_path TEXT PRIMARY KEY,
                  payload TEXT NOT NULL
                )
                """
            )

    def save_repo_root(self, repo_root: Path) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                ("repo_root", str(repo_root)),
            )

    def replace_snapshot(self, repo_root: Path, services, files) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM services")
            conn.execute("DELETE FROM files")
            conn.execute("INSERT OR REPLACE INTO metadata VALUES ('repo_root', ?)", (str(repo_root),))
            conn.executemany("INSERT INTO services VALUES (?,?)", [(s.service_name, s.model_dump_json()) for s in services])
            conn.executemany("INSERT INTO files VALUES (?,?)", [(f.file_path, f.model_dump_json()) for f in files])

    def save_services(self, services: Iterable[ServiceMapping]) -> None:
        with self._conn() as conn:
            for service in services:
                conn.execute(
                    "INSERT OR REPLACE INTO services(service_name, payload) VALUES (?, ?)",
                    (service.service_name, service.model_dump_json()),
                )

    def save_files(self, files: Iterable[FileRecord]) -> None:
        with self._conn() as conn:
            for file in files:
                conn.execute(
                    "INSERT OR REPLACE INTO files(file_path, payload) VALUES (?, ?)",
                    (file.file_path, file.model_dump_json()),
                )

    def get_services(self) -> List[ServiceMapping]:
        with self._conn() as conn:
            rows = conn.execute("SELECT payload FROM services").fetchall()
        return [ServiceMapping.model_validate_json(row["payload"]) for row in rows]

    def get_file_records(self) -> List[FileRecord]:
        with self._conn() as conn:
            rows = conn.execute("SELECT payload FROM files").fetchall()
        return [FileRecord.model_validate_json(row["payload"]) for row in rows]

    def get_file_record(self, file_path: str) -> FileRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT payload FROM files WHERE file_path = ?", (file_path,)).fetchone()
        if row is None:
            return None
        return FileRecord.model_validate_json(row["payload"])

    def summary(self) -> RepoIndexSummary:
        with self._conn() as conn:
            repo_root_row = conn.execute("SELECT value FROM metadata WHERE key = 'repo_root'").fetchone()
            file_count = conn.execute("SELECT COUNT(*) AS c FROM files").fetchone()["c"]
            rows = conn.execute("SELECT payload FROM files").fetchall()
            sym_count = 0
            for row in rows:
                payload = json.loads(row["payload"])
                sym_count += len(payload.get("symbols", []))
        return RepoIndexSummary(
            repo_root=repo_root_row["value"] if repo_root_row else "",
            service_mappings=self.get_services(),
            indexed_files=file_count,
            indexed_symbols=sym_count,
            db_path=str(self.db_path),
        )
