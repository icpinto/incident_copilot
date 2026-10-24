import multiprocessing
import os
import threading
from pathlib import Path

from incident_copilot.indexing import isolated_parser, repo_indexer
from incident_copilot.indexing.isolated_parser import IsolatedTreeSitterFacade
from incident_copilot.indexing.storage import IndexStorage


def _crashing_worker(connection):
    connection.send(True)
    while True:
        path, _source, grammar = connection.recv()
        if grammar == "python":
            # Abrupt process exit bypasses Python exception handlers, like a native fault.
            os._exit(139)
        connection.send([{
            "name": "healthy", "kind": "function", "line_start": 1, "line_end": 1,
            "file_path": path,
        }])


def _hanging_worker(connection):
    connection.send(True)
    connection.recv()
    threading.Event().wait()


def _startup_crashing_worker(connection):
    os._exit(139)


def test_native_runtime_crash_during_startup_uses_fallback(monkeypatch, caplog):
    monkeypatch.setattr(isolated_parser, "_parse_worker", _startup_crashing_worker)
    with IsolatedTreeSitterFacade() as parser:
        assert not parser.available
        assert parser.extract_symbols(Path("main.py"), "def main(): pass", "python") is None
    assert "exitcode=139" in caplog.text


def test_parser_crash_keeps_repository_indexing_alive(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(isolated_parser, "_parse_worker", _crashing_worker)
    root = tmp_path / "repo"
    root.mkdir()
    for name in ("one", "two"):
        (root / f"{name}.py").write_text(f"def {name}():\n    pass\n", encoding="utf-8")
    database = tmp_path / "index.db"

    summary = repo_indexer.index_repository(root, None, database)

    assert summary.indexed_files == summary.indexed_symbols == 2
    records = IndexStorage(database).get_file_records()
    assert {record.symbols[0].name for record in records} == {"one", "two"}
    failures = [record.message for record in caplog.records if "subprocess failed" in record.message]
    assert len(failures) == 1  # Later files use fallback without crashing another parser.
    assert ".py" in failures[0] and "grammar=python" in failures[0]
    assert "exitcode=139" in failures[0]


def test_parser_crash_preserves_other_grammars_and_cleans_up(monkeypatch):
    monkeypatch.setattr(isolated_parser, "_parse_worker", _crashing_worker)
    with IsolatedTreeSitterFacade() as parser:
        assert parser.extract_symbols(Path("bad.py"), "def bad(): pass", "python") is None
        assert parser.extract_symbols(Path("again.py"), "def bad(): pass", "python") is None
        symbols = parser.extract_symbols(Path("healthy.js"), "function healthy() {}", "javascript")
        assert [symbol.name for symbol in symbols] == ["healthy"]
        child_pid = parser._process.pid
    assert child_pid not in {process.pid for process in multiprocessing.active_children()}


def test_parser_timeout_terminates_child_and_uses_fallback(monkeypatch, caplog):
    monkeypatch.setattr(isolated_parser, "_parse_worker", _hanging_worker)
    with IsolatedTreeSitterFacade(timeout_seconds=0.1) as parser:
        child_pid = parser._process.pid
        assert parser.extract_symbols(Path("slow.ts"), "const x = 1", "typescript") is None
        assert parser.extract_symbols(Path("next.ts"), "const x = 2", "typescript") is None
    assert child_pid not in {process.pid for process in multiprocessing.active_children()}
    assert "slow.ts" in caplog.text and "TimeoutError" in caplog.text


def test_disabling_tree_sitter_avoids_loading_native_parser(tmp_path, monkeypatch):
    monkeypatch.setenv("IC_TREE_SITTER_ENABLED", "false")

    def unexpected_start(*args):
        raise AssertionError("A disabled native parser must never start")

    monkeypatch.setattr(IsolatedTreeSitterFacade, "_start", unexpected_start)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("def charge():\n    pass\n", encoding="utf-8")
    database = tmp_path / "index.db"
    summary = repo_indexer.index_repository(root, None, database)
    assert summary.indexed_symbols == 1
    assert IndexStorage(database).get_file_record("main.py").symbols[0].name == "charge"
