import copy
import sqlite3

import pytest
from pydantic import ValidationError

from incident_copilot.analysis import code_graph
from incident_copilot.analysis import evidence_tools as retrieval
from incident_copilot.core.settings import Settings
from incident_copilot.indexing.storage import IndexStorage
from incident_copilot.schemas.index import FileRecord, SymbolRecord
from incident_copilot.schemas.investigation import EvidenceRequest, SymbolReference

REVISION = "a" * 40


@pytest.fixture
def tools(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    source = (
        "def get():\n    return _path()\n\n"
        "def _path():\n    raise ValueError('cache path missing')\n\n"
        "PASSWORD = 'private-password-value'\nAPI_KEY = 'configured-key-value'\n"
    )
    (root / "cache.py").write_text(source, encoding="utf-8")
    (root / "other.py").write_text("def get():\n    pass\n", encoding="utf-8")
    records = [
        FileRecord(file_path="cache.py", language="python", imports=["pathlib"],
                   env_refs=["CACHE_ROOT"], service_hints=["cache"], symbols=[
                       SymbolRecord(name="get", kind="function", file_path="cache.py", line_start=1, line_end=2),
                       SymbolRecord(name="_path", kind="function", file_path="cache.py", line_start=4, line_end=5),
                   ]),
        FileRecord(file_path="other.py", language="python", symbols=[
            SymbolRecord(name="get", kind="function", file_path="other.py", line_start=1, line_end=2),
        ]),
    ]
    db = tmp_path / "index.db"
    storage = IndexStorage(db)
    storage.replace_snapshot(root, [], records, REVISION)
    monkeypatch.setattr(retrieval, "clean_revision", lambda _: REVISION)
    monkeypatch.setattr(code_graph, "clean_revision", lambda _: REVISION)
    settings = Settings(_env_file=None, code_graph_enabled=True, openai_compatible_api_key="configured-key-value")
    return retrieval.EvidenceTools(root, db, settings, {
        "repository": str(root), "revision": REVISION,
        "index_root": str(root), "index_revision": REVISION,
    })


def request(tool, **kwargs):
    return EvidenceRequest(tool=tool, question="What supports the cache hypothesis?", **kwargs)


def get_symbol():
    return SymbolReference(file_path="cache.py", name="get", line_start=1, line_end=2)


def test_read_only_index_never_creates_or_changes_storage(tmp_path):
    missing = tmp_path / "absent" / "index.db"
    with pytest.raises(FileNotFoundError):
        IndexStorage(missing, read_only=True)
    assert not missing.parent.exists()
    writable = IndexStorage(tmp_path / "exists.db")
    read_only = IndexStorage(writable.db_path, read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        read_only.save_repo_root(tmp_path)


@pytest.mark.asyncio
async def test_symbol_search_keeps_ambiguous_matches_and_filters(tools):
    result = await tools.execute(request("find_symbols", query="get"), "r1")
    assert result.status == "ok"
    assert result.evidence[0].data["ambiguous"] is True
    assert len(result.evidence[0].data["symbols"]) == 2
    result = await tools.execute(request("find_symbols", query="get", service="cache", language="python"), "r2")
    assert [item["file_path"] for item in result.evidence[0].data["symbols"]] == ["cache.py"]


@pytest.mark.asyncio
async def test_source_citations_have_stable_identity_and_redacted_configuration(tools):
    first = await tools.execute(request("read_source", symbol=get_symbol()), "r1")
    second = await tools.execute(request("read_source", symbol=get_symbol()), "r2")
    assert first.evidence[0].id == second.evidence[0].id
    assert first.evidence[0].content == "def get():\n    return _path()"
    assert first.evidence[0].line_start == 1 and first.evidence[0].line_end == 2
    full = await tools.execute(request("read_source", file_path="cache.py"), "r3")
    serialized = full.model_dump_json()
    assert "private-password-value" not in serialized and "configured-key-value" not in serialized
    assert "[REDACTED]" in serialized


@pytest.mark.asyncio
async def test_literal_search_scopes_to_index_and_never_exposes_secret_values(tools):
    (tools.root / "unindexed.py").write_text("cache path missing", encoding="utf-8")
    result = await tools.execute(request("search_source", query="cache path missing"), "r1")
    assert len(result.evidence) == 1
    assert result.evidence[0].file_path == "cache.py" and result.evidence[0].line_start == 5
    assert (await tools.execute(request("search_source", query=".*"), "r2")).status == "empty"
    assert (await tools.execute(request("search_source", query="private-password-value"), "r3")).status == "empty"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../cache.py", "/cache.py", "C:/cache.py", "cache.py\\x", "unindexed.py"])
async def test_untrusted_paths_are_rejected(tools, path):
    (tools.root / "unindexed.py").write_text("pass", encoding="utf-8")
    result = await tools.execute(request("read_source", file_path=path), "r1")
    assert result.status == "invalid" and result.evidence == []


@pytest.mark.asyncio
async def test_unverified_duplicate_symbol_range_is_rejected(tools):
    symbol = get_symbol().model_copy(update={"line_start": 4, "line_end": 5})
    result = await tools.execute(request("read_source", symbol=symbol), "r1")
    assert result.status == "invalid" and result.reason == "unverified_symbol"


@pytest.mark.asyncio
async def test_source_change_discards_retrieved_evidence(tools, monkeypatch):
    revisions = iter([REVISION, "b" * 40])
    monkeypatch.setattr(retrieval, "clean_revision", lambda _: next(revisions))
    result = await tools.execute(request("read_source", symbol=get_symbol()), "r1")
    assert result.reason == "source_changed" and result.evidence == []
    with pytest.raises(ValueError, match="source_changed"):
        monkeypatch.setattr(retrieval, "clean_revision", lambda _: None)
        tools.validate_source()


@pytest.mark.asyncio
async def test_read_source_clips_lines_and_exposes_continuation(tools):
    path = tools.root / "cache.py"
    path.write_text("\n".join("x = 1" for _ in range(200)), encoding="utf-8")
    first = await tools.execute(request("read_source", file_path="cache.py", line_start=1, line_end=200), "r1")
    item = first.evidence[0]
    assert item.truncated and item.line_end == 80 and item.data["next_line"] == 81
    assert len(first.model_dump_json().encode("utf-8")) <= retrieval.MAX_TOOL_BYTES


@pytest.mark.asyncio
async def test_file_context_contains_navigation_metadata(tools):
    result = await tools.execute(request("get_file_context", file_path="cache.py"), "r1")
    item = result.evidence[0]
    assert item.data["imports"] == ["pathlib"]
    assert item.data["env_refs"] == ["CACHE_ROOT"]
    assert item.data["symbols"][1]["name"] == "_path"


@pytest.mark.asyncio
async def test_graph_direction_generation_and_resume_capacity(tools, monkeypatch):
    anchor = dict(get_symbol().model_dump(), uid="get", kind="function")
    helper = dict(anchor, name="_path", uid="path", line_start=4, line_end=5)
    caller = dict(anchor, name="caller", uid="caller")
    graph = {
        "provider": "gitnexus", "repository": str(tools.root), "repo_revision": REVISION,
        "indexed_revision": REVISION, "indexed_at": "generation-1", "provider_version": "1.6.12",
        "status": "partial", "reason": None, "completeness": "lower_bound",
        "symbols": [anchor, helper, caller], "relationships": [
            {"source_uid": "get", "target_uid": "path", "relationship": "CALLS"},
            {"source_uid": "caller", "target_uid": "get", "relationship": "CALLS"},
        ], "seeds": [], "limitations": ["static_paths_do_not_prove_runtime_execution"], "retrieval_ms": 1,
    }
    generations = []

    async def lookup(*args, expected_generation=None):
        generations.append(expected_generation)
        return copy.deepcopy(graph)

    monkeypatch.setattr(retrieval, "retrieve_symbol_context", lookup)
    result = await tools.execute(request("get_related_calls", symbol=get_symbol(), direction="outgoing"), "r1")
    assert [symbol["name"] for symbol in result.graph_context["symbols"]] == ["get", "_path"]
    assert result.evidence[0].data["completeness"] == "lower_bound"
    assert tools.snapshot["graph_generation"] == {"lastCommit": REVISION, "indexedAt": "generation-1"}
    restored = retrieval.EvidenceTools(tools.root, tools.index_db_path, tools.settings, dict(tools.snapshot))
    restored.restore_graph_symbols(tools.graph_symbol_ids)
    await restored.execute(request("get_related_calls", symbol=get_symbol()), "r2")
    assert generations == [None, "generation-1"]
    assert len(restored.graph_symbol_ids) == 2


@pytest.mark.asyncio
async def test_cumulative_graph_budget_does_not_reset_each_request(tools, monkeypatch):
    anchor = dict(get_symbol().model_dump(), uid="get", kind="function")
    counter = 0

    async def lookup(*args, **kwargs):
        nonlocal counter
        counter += 1
        neighbors = [dict(anchor, name=f"n{counter}_{i}", uid=f"n{counter}_{i}") for i in range(8)]
        return {
            "status": "available", "indexed_at": "fixed", "completeness": "bounded", "limitations": [],
            "symbols": [anchor] + neighbors,
            "relationships": [{"source_uid": "get", "target_uid": item["uid"]} for item in neighbors],
        }

    monkeypatch.setattr(retrieval, "retrieve_symbol_context", lookup)
    for number in range(8):
        result = await tools.execute(request("get_related_calls", symbol=get_symbol()), f"r{number}")
        assert len(result.model_dump_json().encode("utf-8")) <= retrieval.MAX_TOOL_BYTES
    assert len(tools.graph_symbol_ids) == 20
    assert result.evidence[0].truncated


@pytest.mark.asyncio
async def test_graph_refresh_stops_resumed_investigation(tools, monkeypatch):
    tools.snapshot["graph_generation"] = {"lastCommit": REVISION, "indexedAt": "old-generation"}

    async def lookup(*args, expected_generation=None):
        assert expected_generation == "old-generation"
        return {"status": "skipped", "reason": "stale_index", "symbols": [], "relationships": []}

    monkeypatch.setattr(retrieval, "retrieve_symbol_context", lookup)
    result = await tools.execute(request("get_related_calls", symbol=get_symbol()), "r1")
    assert result.status == "invalid" and result.reason == "source_changed"
    assert result.evidence == [] and result.graph_context is None


def test_graph_evidence_identity_ignores_request_timing(tools):
    first = tools._item("code_graph", "r1", data={"retrieval_ms": 1, "symbols": [get_symbol().model_dump()]})
    second = tools._item("code_graph", "r2", data={"retrieval_ms": 500, "symbols": [get_symbol().model_dump()]})
    assert first.id == second.id


@pytest.mark.asyncio
async def test_index_replaced_with_another_snapshot_discards_results(tools):
    storage = IndexStorage(tools.index_db_path)
    storage.replace_snapshot(tools.root, [], storage.get_file_records(), "b" * 40)
    result = await tools.execute(request("read_source", symbol=get_symbol()), "r1")
    assert result.status == "invalid" and result.reason == "source_snapshot_mismatch"


@pytest.mark.asyncio
async def test_indexed_symlink_cannot_escape_repository(tools, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("private outside content", encoding="utf-8")
    alias = tools.root / "alias.py"
    try:
        alias.symlink_to(outside)
    except OSError:
        pytest.skip("This Windows account cannot create symlinks")
    storage = IndexStorage(tools.index_db_path)
    storage.save_files([FileRecord(file_path="alias.py")])
    result = await tools.execute(request("read_source", file_path="alias.py"), "r1")
    assert result.status == "invalid" and result.reason == "invalid_source_location"


@pytest.mark.asyncio
async def test_large_unicode_source_response_is_bounded(tools):
    (tools.root / "cache.py").write_text("\n".join("message = '" + "€" * 150 + "'" for _ in range(80)), encoding="utf-8")
    result = await tools.execute(request("read_source", file_path="cache.py", line_end=80), "r1")
    assert result.evidence[0].truncated
    assert len(result.model_dump_json().encode("utf-8")) <= retrieval.MAX_TOOL_BYTES


@pytest.mark.asyncio
async def test_final_envelope_clipping_preserves_source_citations_and_continuation(tools, monkeypatch):
    lines = [f"row{number:04d} = '" + "x" * 90 + "'" for number in range(80)]
    (tools.root / "cache.py").write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setattr(retrieval, "MAX_TOOL_BYTES", 2200)
    result = await tools.execute(request("read_source", file_path="cache.py", line_end=80), "r1")
    item = result.evidence[0]
    assert item.truncated and item.line_end < 80
    assert item.content == "\n".join(lines[item.line_start - 1:item.line_end])
    assert item.data["next_line"] == item.line_end + 1
    continuation = await tools.execute(request("read_source", file_path="cache.py", line_start=item.data["next_line"]), "r2")
    assert continuation.evidence[0].content.splitlines()[0] == lines[item.line_end]


def test_shared_redaction_preserves_physical_lines_and_nested_data(tools):
    private_key = "-----BEGIN RSA PRIVATE KEY-----\nfirst-private-line\nsecond-private-line\n-----END RSA PRIVATE KEY-----"
    source = "before\n" + private_key + "\nPASSWORD = 'hardcoded-secret'\nafter"
    clean = retrieval.redact_evidence({"nested": [source, "https://person:credential@example.invalid"]}, tools.settings)
    redacted = clean["nested"][0]
    assert redacted.count("\n") == source.count("\n")
    assert redacted.splitlines()[-1] == "after"
    assert "first-private-line" not in redacted and "hardcoded-secret" not in redacted
    assert "person:credential" not in clean["nested"][1]


@pytest.mark.asyncio
async def test_search_and_interior_reads_cannot_expose_private_key_body(tools):
    (tools.root / "cache.py").write_text(
        "-----BEGIN RSA PRIVATE KEY-----\nprivate-key-body-line\n-----END RSA PRIVATE KEY-----\nafter\n",
        encoding="utf-8",
    )
    search = await tools.execute(request("search_source", query="private-key-body-line"), "r1")
    assert search.status == "empty"
    interior = await tools.execute(request("read_source", file_path="cache.py", line_start=2, line_end=4), "r2")
    assert "private-key-body-line" not in interior.model_dump_json()
    assert interior.evidence[0].line_start == 2 and interior.evidence[0].line_end == 4


def test_requests_are_strict_and_cannot_choose_runtime_context():
    with pytest.raises(ValidationError):
        request("search_source", query="x", repo_root="/private")
    with pytest.raises(ValidationError):
        request("get_related_calls")
    with pytest.raises(ValidationError):
        request("read_source", file_path="x.py", line_start=5, line_end=2)


def test_response_cap_includes_clipping_metadata(tools):
    from incident_copilot.schemas.investigation import ToolOutcome

    item = tools._item("file_context", "r", data={"imports": ["x"] * 2400})
    outcome = tools._bound(ToolOutcome(request_id="r", tool="get_file_context", status="ok", evidence=[item]))
    assert outcome.reason == "response_clipped"
    assert len(retrieval._encoded(outcome.model_dump(mode="json"))) <= retrieval.MAX_TOOL_BYTES


@pytest.mark.asyncio
async def test_file_context_exposes_relative_symbol_navigation(tools):
    storage = IndexStorage(tools.index_db_path)
    record = storage.get_file_record("cache.py")
    for symbol in record.symbols:
        symbol.file_path = str(tools.root / "cache.py")
    storage.save_files([record])
    outcome = await tools.execute(request("get_file_context", file_path="cache.py"), "relative-navigation")
    assert outcome.status == "ok"
    assert all(symbol["file_path"] == "cache.py" for symbol in outcome.evidence[0].data["symbols"])
