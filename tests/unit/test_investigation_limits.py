"""Controller limits and failure handling independent of a live model/provider."""
import json

import pytest

from incident_copilot.analysis import evidence_tools
from incident_copilot.core.settings import Settings
from incident_copilot.graph import investigation
from incident_copilot.graph.diagnosis_graph import run_diagnosis_graph
from incident_copilot.graph.investigation_runtime import InvestigationRuntime
from incident_copilot.indexing.storage import IndexStorage
from incident_copilot.schemas.index import FileRecord
from incident_copilot.schemas.investigation import EvidenceItem, ToolOutcome

OUTCOME = {
    "diagnosis_status": "provisional",
    "diagnosis_reason": "The incident evidence supports this provisional causal mechanism.",
    "next_checks": ["Verify against the failing runtime input"],
}


REVISION = "a" * 40


@pytest.fixture
def execution(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    (root / "cache.py").write_text("first = 1\nsecond = 2\nthird = 3\n", encoding="utf-8")
    index = tmp_path / "index.sqlite"
    IndexStorage(index).replace_snapshot(root, [], [FileRecord(file_path="cache.py")], REVISION)
    monkeypatch.setattr(investigation, "clean_revision", lambda _: REVISION)
    monkeypatch.setattr(evidence_tools, "clean_revision", lambda _: REVISION)
    settings = Settings(_env_file=None, agentic_enabled=True, data_dir=tmp_path / "data", index_db_path=index)
    payload = {"incident_id": "limits", "parsed_log": {"services": ["cache"],
               "exception_types": ["ValueError"], "error_lines": ["cache failed"]}, "candidates": []}
    return settings, payload, InvestigationRuntime(root, index, "limits-execution")


def decision(prompt, requests=None):
    shown = json.loads(prompt)["evidence"]
    return json.dumps({
        **OUTCOME,
        "action": "retrieve" if requests else "finish", "requests": requests or [], "unresolved_gaps": [],
        "hypotheses": [{"id": "h1", "rank": 1, "title": "Investigate cache configuration",
                        "explanation": "The log identifies the cache as the incident location.",
                        "evidence": ["Incident log"], "confidence": .4,
                        "next_checks": ["Inspect the cache settings"],
                        "evidence_refs": [shown[0]["id"]], "contradicting_evidence_refs": []}],
    })


def read(line):
    return {"tool": "read_source", "question": "Which configuration is defined here?",
            "file_path": "cache.py", "line_start": line, "line_end": line}


@pytest.mark.asyncio
async def test_tool_limit_is_distinct_from_round_limit(execution, monkeypatch):
    settings, payload, runtime = execution
    settings = settings.model_copy(update={"agentic_max_tool_requests": 1})
    calls = 0

    class Model:
        async def complete(self, **kwargs):
            nonlocal calls
            calls += 1
            return decision(kwargs["prompt"], [read(calls)])

    monkeypatch.setattr(investigation, "get_client", lambda *_, **_kwargs: Model())
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert result["investigation"]["stop_reason"] == "tool_call_limit"
    assert result["investigation"]["tool_calls"] == 1
    assert result["investigation"]["rounds"] == 1
    assert result["investigation"]["model_calls"] == 2


@pytest.mark.asyncio
async def test_model_limit_is_enforced_before_another_completion(execution, monkeypatch):
    settings, payload, runtime = execution
    monkeypatch.setattr(investigation, "MAX_MODEL_CALLS", 1)
    calls = 0

    class Model:
        async def complete(self, **kwargs):
            nonlocal calls
            calls += 1
            return decision(kwargs["prompt"], [read(1)])

    monkeypatch.setattr(investigation, "get_client", lambda *_, **_kwargs: Model())
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert calls == 1 and result["investigation"]["model_calls"] == 1
    assert result["investigation"]["stop_reason"] == "model_call_limit"
    assert result["engine"] == "deterministic+llm"


@pytest.mark.asyncio
async def test_content_limit_rejects_excess_evidence_but_keeps_hypothesis(execution, monkeypatch):
    settings, payload, runtime = execution
    monkeypatch.setattr(investigation, "MAX_EVIDENCE_BYTES", 1800)

    class Model:
        async def complete(self, **kwargs):
            return decision(kwargs["prompt"], [read(1)])

    async def retrieve(self, request, request_id):
        return ToolOutcome(request_id=request_id, tool=request.tool, status="ok", evidence=[EvidenceItem(
            id="oversized-evidence", kind="source", repository=str(runtime.repo_root), revision=REVISION,
            file_path="cache.py", line_start=1, line_end=1, content="x" * 3000, request_id=request_id,
        )])

    monkeypatch.setattr(investigation, "get_client", lambda *_, **_kwargs: Model())
    monkeypatch.setattr(evidence_tools.EvidenceTools, "execute", retrieve)
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert result["investigation"]["stop_reason"] == "evidence_limit"
    assert all(item["id"] != "oversized-evidence" for item in result["retrieved_evidence"])
    assert result["engine"] == "deterministic+llm"


@pytest.mark.asyncio
async def test_source_change_during_completion_marks_report_incomplete(execution, monkeypatch):
    settings, payload, runtime = execution

    class Model:
        async def complete(self, **kwargs):
            monkeypatch.setattr(evidence_tools, "clean_revision", lambda _: "b" * 40)
            return decision(kwargs["prompt"])

    monkeypatch.setattr(investigation, "get_client", lambda *_, **_kwargs: Model())
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert result["investigation"]["stop_reason"] == "source_changed"
    assert result["investigation"]["incomplete"] is True
    assert result["investigation"]["tool_calls"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["offline", "malformed", "citation"])
async def test_first_model_failure_uses_deterministic_fallback(execution, monkeypatch, failure):
    settings, payload, runtime = execution

    class Model:
        async def complete(self, **kwargs):
            if failure == "offline":
                raise ConnectionError("private-provider-message")
            if failure == "citation":
                answer = json.loads(decision(kwargs["prompt"]))
                answer["hypotheses"][0]["evidence_refs"] = ["invented-id"]
                return json.dumps(answer)
            return "not-json"

    monkeypatch.setattr(investigation, "get_client", lambda *_, **_kwargs: Model())
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert result["engine"] == "deterministic"
    assert result["investigation"]["model_calls"] == 1
    assert result["investigation"]["stop_reason"] == ("llm_request_failed" if failure == "offline" else "llm_invalid_response")
    assert "private-provider-message" not in json.dumps(result)


@pytest.mark.asyncio
async def test_missing_optional_agentic_dependency_has_explicit_fallback(execution, monkeypatch):
    settings, payload, runtime = execution

    async def unavailable(*args, **kwargs):
        raise ImportError("optional-checkpointer-dependency")

    async def deterministic(state, *_args):
        return {"hypotheses": [], "engine": "deterministic"}

    monkeypatch.setattr(investigation, "run_investigation", unavailable)
    monkeypatch.setattr("incident_copilot.graph.diagnosis_graph.build_hypotheses_node", deterministic)
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert result["investigation"]["stop_reason"] == "agentic_dependency_unavailable"
    assert result["investigation"]["incomplete"] is True
