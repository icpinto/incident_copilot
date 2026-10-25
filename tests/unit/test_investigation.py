import json
import time

import pytest

from incident_copilot.core.settings import Settings
from incident_copilot.graph import investigation
from incident_copilot.graph.diagnosis_graph import run_diagnosis_graph
from incident_copilot.graph.investigation_runtime import InvestigationRuntime
from incident_copilot.indexing.storage import IndexStorage
from incident_copilot.schemas.index import FileRecord, SymbolRecord
from incident_copilot.schemas.investigation import EvidenceItem, ToolOutcome


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "cache.py").write_text('def get():\n    return _path()\n\ndef _path():\n    raise ValueError("bad path")\n')
    index = tmp_path / "index.sqlite"
    symbols = [SymbolRecord(name=name, kind="function", file_path="cache.py", line_start=start, line_end=end)
               for name, start, end in (("get", 1, 2), ("_path", 4, 5))]
    IndexStorage(index).replace_snapshot(root, [], [FileRecord(file_path="cache.py", language="python", symbols=symbols)], "a" * 40)
    monkeypatch.setattr(investigation, "clean_revision", lambda _: "a" * 40)
    monkeypatch.setattr("incident_copilot.analysis.evidence_tools.clean_revision", lambda _: "a" * 40)
    settings = Settings(data_dir=tmp_path / "artifacts", index_db_path=index, agentic_enabled=True)
    payload = {"incident_id": "incident", "parsed_log": {"services": ["cache"], "exception_types": ["ValueError"],
                "error_lines": ["ValueError: bad path"]}, "candidate_limit": 10,
               "candidates": [{"file_path": "cache.py", "symbol_name": "get", "score": 20,
                               "reasons": ["stack frame"], "line_start": 1, "line_end": 2}]}
    return root, index, settings, payload, InvestigationRuntime(root, index, "execution")


def response(prompt, requests=None, *, refs=None, title="Cache path handling", gaps=None):
    data = json.loads(prompt)
    return json.dumps({"action": "retrieve" if requests else "finish", "requests": requests or [],
        "unresolved_gaps": gaps or [], "hypotheses": [{"id": "h1", "rank": 1, "title": title,
        "explanation": "The available evidence points to path handling; runtime causation needs confirmation.",
        "evidence": ["Retrieved source and incident logs"], "confidence": 0.6,
        "next_checks": ["Check the input path"], "evidence_refs": refs or [data["evidence"][0]["id"]],
        "contradicting_evidence_refs": []}]})


@pytest.mark.asyncio
async def test_call_discovery_read_source_and_revise_hypothesis(case, monkeypatch):
    root, _index, settings, payload, runtime = case
    calls, prompts = [], []
    original = investigation.EvidenceTools.execute
    async def tools(self, request, request_id):
        calls.append(request.tool)
        if request.tool != "get_related_calls":
            return await original(self, request, request_id)
        return ToolOutcome(request_id=request_id, tool=request.tool, status="ok", evidence=[EvidenceItem(
            id="calls-path", kind="code_graph", repository=str(root), revision="a" * 40,
            data={"symbols": [{"file_path": "cache.py", "name": "_path", "line_start": 4, "line_end": 5}],
                  "relationship": "get calls _path", "completeness": "lower_bound"},
            limitations=["static_paths_do_not_prove_runtime_execution"], request_id=request_id)])
    class Model:
        async def complete(self, **kwargs):
            prompts.append(json.loads(kwargs["prompt"]))
            if len(prompts) == 1:
                return response(kwargs["prompt"], [{"tool": "get_related_calls", "question": "Which helpers does get call?",
                    "symbol": {"file_path": "cache.py", "name": "get", "line_start": 1, "line_end": 2}}])
            if len(prompts) == 2:
                assert any(e["id"] == "calls-path" for e in prompts[-1]["evidence"])
                return response(kwargs["prompt"], [{"tool": "read_source", "question": "Does _path raise the observed exception?",
                    "hypothesis_id": "h1", "symbol": {"file_path": "cache.py", "name": "_path", "line_start": 4, "line_end": 5}}])
            source = next(e for e in prompts[-1]["evidence"] if e["kind"] == "source")
            assert 'raise ValueError("bad path")' in source["content"]
            return response(kwargs["prompt"], refs=[source["id"]], title="_path rejects the supplied path")
    monkeypatch.setattr(investigation, "get_client", lambda _: Model())
    monkeypatch.setattr(investigation.EvidenceTools, "execute", tools)
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert calls == ["get_related_calls", "read_source"]
    assert result["investigation"]["rounds"] == 2
    assert result["investigation"]["model_calls"] == 3
    assert result["investigation"]["tool_calls"] == 2
    assert result["investigation"]["stop_reason"] == "model_finished"
    assert result["hypotheses"][0]["title"] == "_path rejects the supplied path"
    assert result["hypotheses"][0]["evidence_refs"]
    assert result["likely_service"] == "cache" and result["likely_error_class"] == "ValueError"
    assert [c["score"] for c in result["candidates"]] == [20]


@pytest.mark.asyncio
async def test_repeated_requests_stop_without_another_tool_call(case, monkeypatch):
    _, _, settings, payload, runtime = case
    class Model:
        async def complete(self, **kwargs):
            return response(kwargs["prompt"], [{"tool": "read_source", "question": "Read get", "file_path": "cache.py"}])
    monkeypatch.setattr(investigation, "get_client", lambda _: Model())
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert result["investigation"]["stop_reason"] == "repeated_requests"
    assert result["investigation"]["model_calls"] == 2
    assert result["investigation"]["tool_calls"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["malformed", "offline", "unknown_citation"])
async def test_later_model_failure_retains_last_valid_assessment(case, monkeypatch, failure):
    _, _, settings, payload, runtime = case
    count = 0
    class Model:
        async def complete(self, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                return response(kwargs["prompt"], [{"tool": "read_source", "question": "Read get", "file_path": "cache.py"}])
            if failure == "offline":
                raise ConnectionError("do-not-export-provider-errors")
            if failure == "unknown_citation":
                return response(kwargs["prompt"], refs=["invented-id"])
            return "malformed"
    monkeypatch.setattr(investigation, "get_client", lambda _: Model())
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert result["engine"] == "deterministic+llm"
    assert result["hypotheses"][0]["title"] == "Cache path handling"
    assert result["investigation"]["incomplete"]
    assert result["investigation"]["stop_reason"] in {"llm_invalid_response", "llm_request_failed"}
    assert "do-not-export-provider-errors" not in json.dumps(result)


@pytest.mark.asyncio
async def test_empty_localization_can_search_index(case, monkeypatch):
    _, _, settings, payload, runtime = case
    payload["candidates"] = []
    calls = 0
    class Model:
        async def complete(self, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return response(kwargs["prompt"], [{"tool": "search_source", "question": "Where does the error originate?", "query": "bad path"}])
            match = next(e for e in json.loads(kwargs["prompt"])["evidence"] if e["kind"] == "search_match")
            return response(kwargs["prompt"], refs=[match["id"]])
    monkeypatch.setattr(investigation, "get_client", lambda _: Model())
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert result["engine"] == "deterministic+llm" and calls == 2
    assert result["candidates"] == []


@pytest.mark.asyncio
async def test_no_new_evidence_stops(case, monkeypatch):
    _, _, settings, payload, runtime = case
    class Model:
        async def complete(self, **kwargs):
            return response(kwargs["prompt"], [{"tool": "search_source", "question": "Find error", "query": "absent-string"}])
    monkeypatch.setattr(investigation, "get_client", lambda _: Model())
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert result["investigation"]["stop_reason"] == "no_new_evidence"
    assert result["investigation"]["model_calls"] == 1


@pytest.mark.asyncio
async def test_round_limit_and_tool_limit(case, monkeypatch):
    _, _, settings, payload, runtime = case
    settings = settings.model_copy(update={"agentic_max_rounds": 1, "agentic_max_tool_requests": 1})
    count = 0
    class Model:
        async def complete(self, **kwargs):
            nonlocal count
            count += 1
            return response(kwargs["prompt"], [{"tool": "read_source", "question": "Read source", "file_path": "cache.py", "line_start": count}])
    monkeypatch.setattr(investigation, "get_client", lambda _: Model())
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert result["investigation"]["stop_reason"] == "round_limit"
    assert result["investigation"]["tool_calls"] == 1


def test_prompt_budget_counts_schema_and_system(case):
    _, _, settings, payload, _ = case
    snapshot = {"repository": "repo", "revision": "a" * 40}
    state = investigation._bootstrap(payload, snapshot, settings)
    for i in range(30):
        state["evidence_records"][str(i)] = {"id": str(i), "content": "x" * 8000}
    prompt = investigation.build_investigation_prompt(state, {"rounds": 0, "model_calls": 0, "tool_calls": 0})
    assert len(prompt.encode()) + len(investigation.SYSTEM_PROMPT.encode()) + len(investigation.encoded(investigation.strict_schema())) < 32768
    assert json.loads(prompt)["omitted_evidence_ids"]


@pytest.mark.asyncio
async def test_saved_assessment_survives_crash_and_expired_deadline(case, monkeypatch):
    _, _, settings, payload, runtime = case
    calls = 0
    original = investigation.InvestigationStore.put

    class WorkerCrash(BaseException):
        pass

    class Model:
        async def complete(self, **kwargs):
            nonlocal calls
            calls += 1
            return response(kwargs["prompt"])

    def crash_after_saving(self, key, value):
        original(self, key, value)
        if key.startswith("assessment:"):
            raise WorkerCrash()

    monkeypatch.setattr(investigation, "get_client", lambda _: Model())
    monkeypatch.setattr(investigation.InvestigationStore, "put", crash_after_saving)
    with pytest.raises(WorkerCrash):
        await run_diagnosis_graph(payload, settings, runtime)
    monkeypatch.setattr(investigation.InvestigationStore, "put", original)
    journal = investigation.InvestigationStore(settings.data_dir / "investigations" /
        investigation.digest(runtime.execution_id) / "requests.sqlite")
    budget = journal.budget()
    budget["deadline"] = time.time() - 1
    journal.put("budget", budget)
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert result["engine"] == "deterministic+llm"
    assert result["hypotheses"][0]["title"] == "Cache path handling"
    assert result["investigation"]["stop_reason"] == "deadline"
    assert result["investigation"]["model_calls"] == calls == 1
