"""Guardrail decisions must survive graph execution, retries, and public reporting."""

import asyncio
import json

import pytest

from incident_copilot.analysis import evidence_tools
from incident_copilot.core.settings import Settings
from incident_copilot.dashboard.models import IncidentEvidence
from incident_copilot.graph import investigation
from incident_copilot.graph.diagnosis_graph import run_diagnosis_graph
from incident_copilot.graph.investigation_runtime import InvestigationRuntime
from incident_copilot.graph.investigation_store import InvestigationStore
from incident_copilot.graph.nodes.build_hypotheses import (
    build_diagnosis_prompt,
    build_hypotheses_node,
)
from incident_copilot.indexing.storage import IndexStorage
from incident_copilot.llm import client
from incident_copilot.reporting.markdown_renderer import render_markdown
from incident_copilot.reporting.report_builder import build_report
from incident_copilot.schemas.index import FileRecord
from tests.action_plan_fixtures import action_plan, shown_evidence

pytestmark = pytest.mark.asyncio


class PipelineModel:
    def __init__(self, *, inject_at=None, leak_at=None):
        self.classifier_calls = 0
        self.generation_calls = 0
        self.inject_at = inject_at
        self.leak_at = leak_at

    async def complete(self, **kwargs):
        if "injection" in (kwargs.get("response_schema") or {}).get("properties", {}):
            self.classifier_calls += 1
            return json.dumps({"injection": self.classifier_calls == self.inject_at})
        self.generation_calls += 1
        body = {
            "diagnosis_status": "provisional",
            "diagnosis_reason": "Inspect the failing cache input",
            "next_checks": ["Check the failing key"],
            "hypotheses": [
                {
                    "rank": 1,
                    "title": "Cache input requires inspection",
                    "explanation": "The failure points to cache input handling.",
                    "evidence": ["Cache log"],
                    "confidence": 0.5,
                    "next_checks": ["Inspect runtime input"],
                }
            ],
        }
        if self.generation_calls == self.leak_at:
            body["hypotheses"][0]["title"] = "api_key=leaked-secret-sentinel"
        if "action" in kwargs["response_schema"].get("properties", {}):
            evidence = json.loads(kwargs["prompt"])["evidence"]
            body["hypotheses"][0].update(
                id="h1", evidence_refs=[evidence[0]["id"]], contradicting_evidence_refs=[]
            )
            body.update(
                action="retrieve" if self.generation_calls == 1 else "finish",
                unresolved_gaps=[],
                requests=[
                    {
                        "tool": "read_source",
                        "file_path": "cache.py",
                        "question": "How is the cache key used?",
                    }
                ]
                if self.generation_calls == 1
                else [],
            )
        body["hypotheses"][0]["action_plan"] = action_plan([shown_evidence(kwargs["prompt"])[0]["id"]])
        return json.dumps(body)


@pytest.fixture
def execution(tmp_path, monkeypatch):
    pytest.importorskip("nemoguardrails")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "cache.py").write_text("cache_path = '/cache/'\n", encoding="utf-8")
    index = tmp_path / "index.sqlite"
    revision = "a" * 40
    IndexStorage(index).replace_snapshot(root, [], [FileRecord(file_path="cache.py")], revision)
    monkeypatch.setattr(investigation, "clean_revision", lambda _: revision)
    monkeypatch.setattr(evidence_tools, "clean_revision", lambda _: revision)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        index_db_path=index,
        code_graph_enabled=False,
        guardrails_mode="enforce",
        agentic_enabled=True,
    )
    payload = {
        "incident_id": "guarded",
        "parsed_log": {"error_lines": ["Cache failed"], "exception_types": ["ValueError"]},
        "candidates": [
            {
                "file_path": "cache.py",
                "score": 20,
                "reasons": ["Stack frame"],
                "snippet": "cache_path = '/cache/'",
            }
        ],
    }
    runtime = InvestigationRuntime(root, index, "guarded-execution")
    return settings, payload, runtime


@pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
async def test_single_pass_modes_and_public_exports(execution, monkeypatch, mode):
    settings, payload, runtime = execution
    settings = settings.model_copy(update={"guardrails_mode": mode, "agentic_enabled": False})
    model = PipelineModel(inject_at=1)
    monkeypatch.setattr(client, "get_raw_client", lambda _: model)
    updates = []

    async def progress(update):
        updates.append(update)

    runtime = InvestigationRuntime(
        runtime.repo_root, runtime.index_db_path, runtime.execution_id, progress
    )
    state = await run_diagnosis_graph(payload, settings, runtime)
    if mode == "enforce":
        assert model.generation_calls == 0 and state["fallback_reason"] == "guardrail_input_blocked"
        assert state["diagnosis_status"] == "insufficient_evidence"
    else:
        assert model.generation_calls == 1 and state["engine"] == "deterministic+llm"
    report = build_report(state)
    if mode == "off":
        assert report.guardrails is None and model.classifier_calls == 0
    else:
        assert updates and updates[-1]["guardrails"] == state["guardrails"]
        evidence = IncidentEvidence.model_validate(updates[-1])
        assert evidence.guardrails == report.guardrails
        markdown = render_markdown(report)
        assert "## Guardrails" in markdown and "prompt_injection" in markdown
        assert json.loads(report.model_dump_json())["guardrails"]["mode"] == mode
    assert not state["validation_errors"]


@pytest.mark.parametrize("failure", ["input", "output"])
async def test_agentic_rejection_preserves_earlier_accepted_findings(
    execution, monkeypatch, failure
):
    settings, payload, runtime = execution
    model = PipelineModel(
        inject_at=2 if failure == "input" else None, leak_at=2 if failure == "output" else None
    )
    monkeypatch.setattr(client, "get_raw_client", lambda _: model)
    state = await run_diagnosis_graph(payload, settings, runtime)
    assert state["investigation"]["stop_reason"] == f"guardrail_{failure}_blocked"
    assert state["investigation"]["incomplete"]
    assert state["investigation"]["tool_calls"] == 1
    assert state["hypotheses"][0]["title"] == "Cache input requires inspection"
    assert state["engine"] == "deterministic+llm" and not state["validation_errors"]
    report = build_report(state)
    assert "leaked-secret-sentinel" not in report.model_dump_json()
    cached = await run_diagnosis_graph(payload, settings, runtime)
    assert cached == state
    assert model.classifier_calls == 2


async def test_cancel_after_input_check_reuses_verdict_and_keeps_reservations(
    execution, monkeypatch
):
    settings, payload, runtime = execution
    model = PipelineModel()
    monkeypatch.setattr(client, "get_raw_client", lambda _: model)
    entered = asyncio.Event()

    async def progress(update):
        summary = update.get("guardrails")
        if summary and any(
            e["stage"] == "input" and e["status"] == "passed" for e in summary["events"]
        ):
            entered.set()
            await asyncio.Event().wait()

    interruptible = InvestigationRuntime(
        runtime.repo_root, runtime.index_db_path, runtime.execution_id, progress
    )
    task = asyncio.create_task(run_diagnosis_graph(payload, settings, interruptible))
    await asyncio.wait_for(entered.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    directory = settings.data_dir / "investigations" / investigation.digest(runtime.execution_id)
    journal = InvestigationStore(directory / "requests.sqlite")
    before = journal.get("guardrails")["summary"]
    assert before["checks"] == before["model_calls"] == 1
    assert model.generation_calls == 0
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert result["investigation"]["stop_reason"] == "model_finished"
    assert result["guardrails"]["checks"] == 4
    assert result["guardrails"]["model_calls"] == model.classifier_calls == 2
    assert model.generation_calls == 2
    assert journal.budget()["model_calls"] == 3  # The interrupted assessment reservation survives.


async def test_policy_views_exclude_templates_but_include_model_bound_evidence(execution):
    _, payload, _ = execution
    view = {}
    build_diagnosis_prompt(payload, policy_view=view)
    assert "response_schema" not in view
    assert "Cache failed" in json.dumps(view) and "cache_path" in json.dumps(view)
    state = {
        **payload,
        "evidence_records": {"ev1": {"id": "ev1", "kind": "log", "content": "only shown log"}},
    }
    view = {}
    investigation.build_investigation_prompt(
        state, {"rounds": 0, "model_calls": 1, "tool_calls": 0}, policy_view=view
    )
    assert "response_schema" not in view and "task" not in view and "budget" not in view
    assert "only shown log" in json.dumps(view)


async def test_single_pass_output_rejection_keeps_raw_response_out_of_report(
    execution, monkeypatch
):
    settings, payload, _ = execution
    monkeypatch.setattr(client, "get_raw_client", lambda _: PipelineModel(leak_at=1))
    result = await build_hypotheses_node(payload, settings)
    assert result["fallback_reason"] == "guardrail_output_blocked" and not result["hypotheses"]
    assert "leaked-secret-sentinel" not in json.dumps(result)
