"""Integration contracts around trusted context, retries, and Temporal cancellation."""
from __future__ import annotations

import asyncio
import io
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from incident_copilot.core.settings import Settings
from incident_copilot.workflows import temporal_activities, temporal_workflows
from incident_copilot.workflows.activity_heartbeat import reasoning_heartbeat

OUTCOME = {
    "diagnosis_status": "provisional",
    "diagnosis_reason": "The incident evidence supports this provisional causal mechanism.",
    "next_checks": ["Verify against the failing runtime input"],
}


@pytest.mark.parametrize("field,value", [
    ("agentic_max_rounds", 4), ("agentic_max_rounds", 0),
    ("agentic_max_tool_requests", 13), ("agentic_max_tool_requests", 0),
    ("agentic_timeout_seconds", 181), ("agentic_timeout_seconds", 0),
])
def test_agentic_settings_enforce_controller_bounds(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_execution_index_is_stable_isolated_and_contained(tmp_path):
    runtime = Settings(_env_file=None, data_dir=tmp_path, index_db_path=tmp_path / "index.db").model_dump()
    first = temporal_activities.runtime_settings(runtime, {"execution_id": "first"})
    retry = temporal_activities.runtime_settings(runtime, {"execution_id": "first"})
    other = temporal_activities.runtime_settings(runtime, {"execution_id": "second"})
    assert first.index_db_path == retry.index_db_path == tmp_path / "runs" / "first" / "index.db"
    assert other.index_db_path != first.index_db_path
    with pytest.raises(ValueError, match="execution identifier"):
        temporal_activities.runtime_settings(runtime, {"execution_id": "../escape"})


def test_legacy_execution_captures_configuration_for_all_stages(tmp_path, monkeypatch):
    from incident_copilot.dashboard.config import Configuration
    from incident_copilot.dashboard.models import SettingsUpdate
    from incident_copilot.dashboard.store import ControlStore

    monkeypatch.setenv("IC_CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("IC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("IC_INDEX_DB_PATH", str(tmp_path / "index.db"))
    configuration = Configuration(ControlStore(tmp_path / "control.db"))
    configuration.save(SettingsUpdate(values={"top_k_candidates": 8}))
    first = temporal_activities.runtime_settings(context={"execution_id": "frozen"})
    configuration.save(SettingsUpdate(values={"top_k_candidates": 9}))
    retry = temporal_activities.runtime_settings(context={"execution_id": "frozen"})
    later = temporal_activities.runtime_settings(context={"execution_id": "later"})
    assert first.top_k_candidates == retry.top_k_candidates == 8
    assert later.top_k_candidates == 9


@pytest.mark.asyncio
async def test_legacy_reasoning_passes_trusted_context_and_envelope(tmp_path, monkeypatch):
    context = {"repo_root": str(tmp_path), "execution_id": "execution"}
    runtime = Settings(_env_file=None, data_dir=tmp_path, index_db_path=tmp_path / "index.db").model_dump()
    pulses = []
    monkeypatch.setattr(temporal_activities, "activity", SimpleNamespace(heartbeat=lambda: pulses.append(True)))

    async def diagnose(payload, settings, runtime_context=None):
        assert payload["deep_analysis"] == {"main.py": {"imports": []}}
        assert payload["graph_context"] is None
        assert runtime_context.repo_root == tmp_path.resolve()
        assert runtime_context.index_db_path == tmp_path / "runs" / "execution" / "index.db"
        assert runtime_context.execution_id == "execution"
        runtime_context.heartbeat()
        return {**payload, "hypotheses": [], "summary": "Investigated"}

    monkeypatch.setattr(temporal_activities, "run_diagnosis_graph", diagnose)
    result = await temporal_activities.diagnosis_graph_activity(
        "incident", {}, [], {"files": {"main.py": {"imports": []}}, "graph_context": None}, runtime, context,
    )
    assert result["summary"] == "Investigated" and len(pulses) >= 2


@pytest.mark.asyncio
async def test_heartbeat_stays_active_during_await_and_cleans_up_on_cancel():
    pulses = []
    entered = asyncio.Event()

    async def run():
        async with reasoning_heartbeat(SimpleNamespace(heartbeat=lambda: pulses.append(True)), .001):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await entered.wait()
    await asyncio.sleep(.01)
    assert len(pulses) > 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    completed = len(pulses)
    await asyncio.sleep(.01)
    assert len(pulses) == completed


@pytest.mark.asyncio
@pytest.mark.parametrize("versioned", [True, False])
@pytest.mark.parametrize("dashboard", [True, False])
async def test_temporal_patch_preserves_old_history_arguments(tmp_path, monkeypatch, versioned, dashboard):
    calls = []
    identifiers = iter([UUID(int=1), UUID(int=2)])

    async def execute(fn, **kwargs):
        calls.append((fn.__name__, kwargs))
        if fn.__name__ == "correlate_logs_activity":
            return {"parsed_log": {}, "candidates": []}
        return {}

    monkeypatch.setattr(temporal_workflows, "workflow", SimpleNamespace(
        patched=lambda name: versioned, uuid4=lambda: next(identifiers), execute_activity=execute,
    ))
    incident = {"dashboard": {"incident_id": "incident"}} if dashboard else {
        "repo_root": str(tmp_path), "log_text": "ValueError", "incident_id": "incident",
    }
    await temporal_workflows.DiagnoseIncidentWorkflow().run(incident)
    assert len(calls) == 4
    assert all("heartbeat_timeout" not in options for _, options in calls[:2])
    assert ("heartbeat_timeout" in calls[2][1]) == versioned
    assert calls[2][1]["start_to_close_timeout"] == timedelta(minutes=10 if dashboard or versioned else 5)
    if versioned:
        assert calls[-1][1]["heartbeat_timeout"] == timedelta(seconds=20)
    else:
        assert "heartbeat_timeout" not in calls[-1][1]
    if dashboard:
        assert [options["args"] for _, options in calls] == [
            ["incident", stage] for stage in ("indexing", "correlating", "analyzing", "reasoning")
        ]
    elif versioned:
        contexts = [options["args"][-1] for _, options in calls]
        assert all(context == contexts[0] for context in contexts)
        assert contexts[0]["repo_root"] == str(tmp_path)
        assert all(options["args"][-2] is None for _, options in calls)
    else:
        assert [len(options["args"]) for _, options in calls] == [2, 1, 2, 4]


@pytest.mark.asyncio
async def test_dashboard_progress_is_saved_and_survives_final_stage_write(tmp_path, monkeypatch):
    from incident_copilot.dashboard import activities
    from incident_copilot.dashboard.models import IncidentSubmission
    from incident_copilot.dashboard.service import DashboardService
    from incident_copilot.dashboard.store import ControlStore, utcnow

    monkeypatch.setenv("IC_CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("IC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("IC_INDEX_DB_PATH", str(tmp_path / "index.db"))
    service = DashboardService(ControlStore(tmp_path / "control.db"))
    service.store.put("repositories", "repo", {
        "id": "repo", "name": "Repo", "repo_root": str(tmp_path), "compose_path": None,
        "archived": False, "created_at": utcnow(),
    })
    run = service.submit(IncidentSubmission(repository_id="repo", log_text="ValueError"))
    summary = {"rounds": 1, "model_calls": 2, "tool_calls": 1, "stop_reason": "finished"}
    retrieved = [{"id": "evidence", "kind": "source", "repository": str(tmp_path), "revision": "test"}]
    validation_errors = [{"stage": "investigation_assessment", "kind": "json", "model_call": 2,
                          "issues": [{"path": [], "code": "json_invalid", "message": "Response is not valid JSON."}],
                          "total_issues": 1}]

    async def diagnose(payload, settings, runtime_context=None):
        assert runtime_context.execution_id == run.id
        await runtime_context.progress({"investigation": summary, "retrieved_evidence": retrieved,
                                        "validation_errors": validation_errors})
        snapshot = service.store.get("incidents", run.id)
        assert snapshot["evidence"]["raw_log"] == "ValueError"
        assert snapshot["evidence"]["retrieved_evidence"] == retrieved
        assert snapshot["evidence"]["validation_errors"] == validation_errors
        return {**payload, "hypotheses": [], "summary": "Investigated"}

    monkeypatch.setattr(activities, "run_diagnosis_graph", diagnose)
    await activities.dashboard_stage_activity(run.id, "reasoning")
    saved = service.store.get("incidents", run.id)
    assert saved["evidence"]["investigation"] == summary
    assert saved["evidence"]["retrieved_evidence"] == retrieved
    assert saved["evidence"]["validation_errors"] == validation_errors
    assert saved["timeline"][0]["completed_at"]


@pytest.mark.parametrize("entrypoint", ["dashboard", "api", "cli", "legacy"])
def test_enabled_entrypoints_share_investigation_and_initial_analysis(
    tmp_path, monkeypatch, entrypoint,
):
    from incident_copilot.analysis import evidence_tools
    from incident_copilot.dashboard import activities
    from incident_copilot.dashboard.models import IncidentSubmission
    from incident_copilot.dashboard.service import DashboardService
    from incident_copilot.dashboard.store import ControlStore, utcnow
    from incident_copilot.graph import investigation
    from incident_copilot.indexing import repo_indexer

    monkeypatch.setenv("IC_CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("IC_DATA_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IC_INDEX_DB_PATH", str(tmp_path / "index.db"))
    monkeypatch.setenv("IC_AGENTIC_ENABLED", "true")
    monkeypatch.setenv("IC_CODE_GRAPH_ENABLED", "false")
    monkeypatch.setenv("IC_TREE_SITTER_ENABLED", "false")
    for module in (investigation, evidence_tools, repo_indexer):
        monkeypatch.setattr(module, "clean_revision", lambda _: "a" * 40)
    prompts = []

    class Model:
        async def complete(self, **kwargs):
            prompt = json.loads(kwargs["prompt"])
            prompts.append(prompt)
            assert any(e["kind"] == "file_analysis" for e in prompt["evidence"])
            reference = prompt["evidence"][0]["id"]
            return json.dumps({
                **OUTCOME,
                "action": "finish", "requests": [], "unresolved_gaps": [], "hypotheses": [{
                    "id": "failure", "rank": 1, "title": "Inspect charge failure",
                    "explanation": "The indexed charge function raises ValueError.",
                    "evidence": ["The logs and static code identify a charge failure."],
                    "confidence": .6, "next_checks": ["Inspect the incident input."],
                    "evidence_refs": [reference], "contradicting_evidence_refs": [],
                }],
            })

    monkeypatch.setattr(investigation, "get_client", lambda *_, **_kwargs: Model())
    root = tmp_path / "source"
    root.mkdir()
    (root / "main.py").write_text("def charge():\n    raise ValueError('payment failed')\n")
    log = 'api | ValueError: payment failed\nFile "/app/main.py", line 2, in charge'
    service = DashboardService(ControlStore(tmp_path / "control.db"))

    async def run_async():
        if entrypoint == "api":
            from apps.api.main import diagnose
            from incident_copilot.schemas.incident import IncidentInput
            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(dashboard=service)))
            return (await diagnose(IncidentInput(repo_root=root, log_text=log), request)).model_dump()
        if entrypoint == "dashboard":
            service.store.put("repositories", "repo", {
                "id": "repo", "name": "Repo", "repo_root": str(root), "compose_path": None,
                "archived": False, "created_at": utcnow(),
            })
            run = service.submit(IncidentSubmission(repository_id="repo", log_text=log))
            for stage in ("indexing", "correlating", "analyzing", "reasoning"):
                await activities.dashboard_stage_activity(run.id, stage)
            return service.store.get("incidents", run.id)["report"]
        context = {"repo_root": str(root), "execution_id": "legacy-enabled"}
        await temporal_activities.build_service_map_and_index_activity(str(root), None, None, context)
        correlation = await temporal_activities.correlate_logs_activity(
            {"repo_root": str(root), "log_text": log}, None, context,
        )
        deep = await temporal_activities.deep_analysis_activity(
            str(root), correlation["candidates"], None, context,
        )
        return await temporal_activities.diagnosis_graph_activity(
            "incident", correlation["parsed_log"], correlation["candidates"], deep, None, context,
        )

    if entrypoint == "cli":
        from rich.console import Console

        from apps.cli import main
        monkeypatch.setattr(main, "console", Console(file=io.StringIO()))
        output = tmp_path / "report.json"
        main.diagnose(root, log_text=log, output_json=output)
        report = json.loads(output.read_text())
    else:
        report = asyncio.run(run_async())
    assert report["engine"] == "deterministic+llm"
    assert report["investigation"]["stop_reason"] == "model_finished"
    assert report["investigation"]["model_calls"] == 1
    assert report["hypotheses"][0]["evidence_refs"]
    assert report["likely_error_class"] == "ValueError"
    assert len(prompts) == 1
