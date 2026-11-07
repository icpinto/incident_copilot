"""Durable budgets and completed work survive cancellation and worker retries."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from incident_copilot.analysis import evidence_tools
from incident_copilot.core.settings import Settings
from incident_copilot.graph import investigation
from incident_copilot.graph.diagnosis_graph import run_diagnosis_graph
from incident_copilot.graph.investigation_runtime import InvestigationRuntime
from incident_copilot.graph.investigation_store import InvestigationStore
from incident_copilot.indexing.storage import IndexStorage
from incident_copilot.schemas.index import FileRecord, SymbolRecord
from tests.action_plan_fixtures import action_plan

OUTCOME = {
    "diagnosis_status": "provisional",
    "diagnosis_reason": "The incident evidence supports this provisional causal mechanism.",
    "next_checks": ["Verify against the failing runtime input"],
}


class InvestigationModel:
    def __init__(self):
        self.prompts = []

    async def complete(self, **kwargs):
        prompt = json.loads(kwargs["prompt"])
        self.prompts.append(prompt)
        source = next((e for e in prompt["evidence"] if e["kind"] == "source"), None)
        return json.dumps({
            **OUTCOME,
            "action": "finish" if source else "retrieve",
            "hypotheses": [{
                "id": "cache-path", "rank": 1, "title": "Cache path needs inspection",
                "explanation": "Retrieved source identifies the cache path handling to inspect.",
                "evidence": ["Static source evidence; execution is unproven."],
                "confidence": .6, "next_checks": ["Compare the runtime cache directory."],
                "evidence_refs": [source["id"] if source else prompt["evidence"][0]["id"]],
                "contradicting_evidence_refs": [], "action_plan": action_plan([source["id"] if source else prompt["evidence"][0]["id"]]),
            }],
            "unresolved_gaps": [],
            "requests": [] if source else [{
                "tool": "read_source", "question": "How is the cache path constructed?",
                "file_path": "cache.py", "line_start": 1, "line_end": 4,
            }],
        })


@pytest.fixture
def execution(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    source = root / "cache.py"
    source.write_text("def get(key):\n    return _path(key)\ndef _path(key):\n    return '/cache/' + key\n")
    revision = "a" * 40
    index = tmp_path / "index.db"
    IndexStorage(index).replace_snapshot(root, [], [FileRecord(
        file_path="cache.py", language="python", size_bytes=source.stat().st_size,
        mtime=source.stat().st_mtime, symbols=[
            SymbolRecord(name="get", kind="function", file_path="cache.py", line_start=1, line_end=2),
            SymbolRecord(name="_path", kind="function", file_path="cache.py", line_start=3, line_end=4),
        ],
    )], revision)
    monkeypatch.setattr(investigation, "clean_revision", lambda _: revision)
    monkeypatch.setattr(evidence_tools, "clean_revision", lambda _: revision)
    model = InvestigationModel()
    monkeypatch.setattr(investigation, "get_client", lambda *_, **_kwargs: model)
    settings = Settings(_env_file=None, agentic_enabled=True, code_graph_enabled=False,
                        data_dir=tmp_path / "artifacts", index_db_path=index)
    runtime = InvestigationRuntime(root, index, "recoverable-execution")
    payload = {"incident_id": "incident", "parsed_log": {
        "services": ["cache"], "exception_types": ["OSError"], "error_lines": ["cache read failed"],
    }, "candidates": [], "deep_analysis": {}}
    def journal():
        directory = settings.data_dir / "investigations" / investigation.digest(runtime.execution_id)
        return InvestigationStore(directory / "requests.sqlite")
    return settings, runtime, payload, model, journal


async def interrupt_after_retrieval(payload, settings, runtime):
    retrieved = asyncio.Event()

    async def progress(update):
        if any(event["kind"] == "retrieval" for event in update["investigation_events"]):
            retrieved.set()
            await asyncio.Event().wait()

    callbacks = InvestigationRuntime(runtime.repo_root, runtime.index_db_path, runtime.execution_id, progress)
    task = asyncio.create_task(run_diagnosis_graph(payload, settings, callbacks))
    await asyncio.wait_for(retrieved.wait(), timeout=5)
    # Signal the outer activity task, as Temporal does; a node-raised
    # CancelledError is intentionally classified differently by LangGraph.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_resume_reuses_completed_retrieval_and_completed_report(execution):
    settings, runtime, payload, model, journal = execution
    await interrupt_after_retrieval(payload, settings, runtime)
    reserved = journal().budget()
    assert reserved["rounds"] == reserved["model_calls"] == reserved["tool_calls"] == 1
    assert journal().get("final") is None

    report = await run_diagnosis_graph(payload, settings, runtime)
    assert report["investigation"]["stop_reason"] == "model_finished"
    assert report["investigation"]["model_calls"] == 2
    assert report["investigation"]["tool_calls"] == 1
    assert len(model.prompts) == 2
    assert len([event for event in report["investigation_events"] if event["kind"] == "retrieval"]) == 1
    source = next(e for e in report["retrieved_evidence"] if e["kind"] == "source")
    assert report["hypotheses"][0]["evidence_refs"] == [source["id"]]
    consumed = journal().budget()

    cached = await run_diagnosis_graph({**payload, "parsed_log": {}}, settings, runtime)
    assert cached == report
    assert journal().budget() == consumed
    assert len(model.prompts) == 2


@pytest.mark.asyncio
async def test_retry_deadline_includes_time_while_worker_is_down(execution):
    settings, runtime, payload, model, journal = execution
    await interrupt_after_retrieval(payload, settings, runtime)
    budget = journal().budget()
    original_start = budget["started_at"]
    budget["deadline"] = time.time() - 1
    journal().put("budget", budget)

    report = await run_diagnosis_graph(payload, settings, runtime)
    assert report["investigation"]["stop_reason"] == "deadline"
    assert report["investigation"]["incomplete"]
    assert journal().budget()["started_at"] == original_start
    assert journal().budget()["model_calls"] == journal().budget()["tool_calls"] == 1
    assert len(model.prompts) == 1


@pytest.mark.asyncio
async def test_abstention_survives_interruption_and_expired_deadline(execution, monkeypatch):
    settings, runtime, payload, model, journal = execution
    original = model.complete

    async def abstain(**kwargs):
        data = json.loads(await original(**kwargs))
        data.update(diagnosis_status="insufficient_evidence", diagnosis_reason="Runtime input is missing",
                    next_checks=["Capture the failing runtime input"], hypotheses=[])
        return json.dumps(data)

    monkeypatch.setattr(model, "complete", abstain)
    await interrupt_after_retrieval(payload, settings, runtime)
    budget = journal().budget()
    budget["deadline"] = time.time() - 1
    journal().put("budget", budget)
    report = await run_diagnosis_graph(payload, settings, runtime)
    assert report["diagnosis_status"] == "insufficient_evidence" and report["hypotheses"] == []
    assert report["next_checks"] == ["Capture the failing runtime input"]
    assert report["engine"] == "deterministic+llm" and report["fallback_reason"] is None
    assert report["investigation"]["stop_reason"] == "deadline"
    assert report["investigation"]["incomplete"] and len(model.prompts) == 1


@pytest.mark.asyncio
async def test_cancel_during_model_retains_reservation_and_propagates(execution, monkeypatch):
    settings, runtime, payload, model, journal = execution
    entered = asyncio.Event()

    class SlowModel:
        async def complete(self, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(investigation, "get_client", lambda *_, **_kwargs: SlowModel())
    task = asyncio.create_task(run_diagnosis_graph(payload, settings, runtime))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert journal().budget()["model_calls"] == 1
    assert journal().get("final") is None

    monkeypatch.setattr(investigation, "get_client", lambda *_, **_kwargs: model)
    report = await run_diagnosis_graph(payload, settings, runtime)
    assert report["investigation"]["model_calls"] == 3
    assert report["investigation"]["tool_calls"] == 1
    assert len(model.prompts) == 2


@pytest.mark.asyncio
async def test_cancel_during_retrieval_can_repeat_only_interrupted_external_work(execution, monkeypatch):
    settings, runtime, payload, _model, journal = execution
    entered = asyncio.Event()
    execute = evidence_tools.EvidenceTools.execute

    async def slow_execute(*_):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(evidence_tools.EvidenceTools, "execute", slow_execute)
    task = asyncio.create_task(run_diagnosis_graph(payload, settings, runtime))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert journal().budget()["tool_calls"] == 1

    monkeypatch.setattr(evidence_tools.EvidenceTools, "execute", execute)
    report = await run_diagnosis_graph(payload, settings, runtime)
    assert report["investigation"]["tool_calls"] == 2
    assert report["investigation"]["model_calls"] == 2
    assert len([e for e in report["retrieved_evidence"] if e["kind"] == "source"]) == 1


@pytest.mark.asyncio
async def test_overlapping_attempts_serialize_one_execution(execution, monkeypatch):
    settings, runtime, payload, model, journal = execution
    original_complete = model.complete

    async def delayed_complete(**kwargs):
        await asyncio.sleep(.01)
        return await original_complete(**kwargs)

    monkeypatch.setattr(model, "complete", delayed_complete)
    first, second = await asyncio.gather(
        run_diagnosis_graph(payload, settings, runtime), run_diagnosis_graph(payload, settings, runtime),
    )
    assert first == second
    assert len(model.prompts) == 2
    assert journal().budget()["model_calls"] == 2
    assert journal().budget()["tool_calls"] == 1
