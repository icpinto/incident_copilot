"""Timeline delivery contracts across the existing execution entrypoints."""
import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from incident_copilot.dashboard.activities import dashboard_stage_activity
from incident_copilot.dashboard.models import IncidentSubmission
from incident_copilot.dashboard.service import DashboardService
from incident_copilot.dashboard.store import ControlStore, utcnow
from incident_copilot.graph.nodes import build_hypotheses
from incident_copilot.workflows import temporal_activities
from incident_copilot.workflows.log_context import hydrate_parsed_log

FIXTURE = Path(__file__).parent / "fixtures" / "incident_timeline" / "correlated.jsonl"


@pytest.mark.parametrize("entrypoint", ["dashboard", "api", "cli", "temporal"])
def test_timeline_survives_delivery_and_retries(tmp_path, monkeypatch, entrypoint):
    monkeypatch.setenv("IC_CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("IC_DATA_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IC_INDEX_DB_PATH", str(tmp_path / "index.db"))
    monkeypatch.setenv("IC_AGENTIC_ENABLED", "false")
    monkeypatch.setenv("IC_CODE_GRAPH_ENABLED", "false")
    service = DashboardService(ControlStore(tmp_path / "control.db"))
    root = tmp_path / "source"
    for name, file, symbol in (("database", "query", "query"), ("api", "handler", "handle"), ("worker", "job", "job")):
        path = root / "services" / name / f"{file}.py"
        path.parent.mkdir(parents=True)
        path.write_text(f"def {symbol}():\n    raise RuntimeError('failure')\n")
    logs = FIXTURE.read_text()

    class Offline:
        async def complete(self, **kwargs):
            assert "unrelated-sentinel" not in kwargs["prompt"]
            raise ConnectionError("offline")

    monkeypatch.setattr(build_hypotheses, "get_client", lambda *_, **_kwargs: Offline())

    async def execute():
        if entrypoint == "api":
            from apps.api.main import diagnose
            from incident_copilot.schemas.incident import IncidentInput
            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(dashboard=service)))
            return (await diagnose(IncidentInput(repo_root=root, log_text=logs), request)).model_dump()
        if entrypoint == "dashboard":
            service.store.put("repositories", "repo", {"id": "repo", "name": "Repo", "repo_root": str(root),
                                                     "compose_path": None, "created_at": utcnow(), "archived": False})
            run = service.submit(IncidentSubmission(repository_id="repo", log_text=logs))
            for stage in ("indexing", "correlating", "analyzing", "reasoning"):
                await dashboard_stage_activity(run.id, stage)
                if stage == "correlating":
                    original = service.store.get("incidents", run.id)["evidence"]["parsed_log"]
                    await dashboard_stage_activity(run.id, stage)
                    assert service.store.get("incidents", run.id)["evidence"]["parsed_log"] == original
            saved = service.store.get("incidents", run.id)
            assert saved["evidence"]["raw_log"] == logs
            assert saved["evidence"]["parsed_log"]["raw_text"] == ""
            assert len(saved["timeline"]) == 4
            artifact = Path(saved["artifact_dir"])
            assert "Incident Timeline" in (artifact / "report.md").read_text(encoding="utf-8")
            assert json.loads((artifact / "report.json").read_text())["incident_context"] == saved["report"]["incident_context"]
            return saved["report"]
        context = {"repo_root": str(root), "execution_id": "legacy-timeline"}
        await temporal_activities.build_service_map_and_index_activity(str(root), None, None, context)
        # Exercise the same offloading branch used by a real Temporal worker.
        monkeypatch.setattr(temporal_activities, "activity", SimpleNamespace(in_activity=lambda: True, heartbeat=lambda: None))
        inputs = {"repo_root": str(root), "log_text": logs}
        first = await temporal_activities.correlate_logs_activity(inputs, None, context)
        assert first == await temporal_activities.correlate_logs_activity(inputs, None, context)
        assert len(json.dumps(first["parsed_log"])) < 150
        assert "unrelated-sentinel" not in json.dumps(first)
        deep = await temporal_activities.deep_analysis_activity(str(root), first["candidates"], None, context)
        report = await temporal_activities.diagnosis_graph_activity("incident", first["parsed_log"], first["candidates"], deep, None, context)
        assert "incident_context" not in report and "incident_context_ref" in report
        report["incident_context"] = hydrate_parsed_log({"incident_context_ref": report["incident_context_ref"]})["incident_context"]
        return report

    if entrypoint == "cli":
        from apps.cli import main
        monkeypatch.setattr(main, "console", Console(file=io.StringIO()))
        output = tmp_path / "report.json"
        main.diagnose(root, log_text=logs, output_json=output)
        report = json.loads(output.read_text())
    else:
        report = asyncio.run(execute())
    context = report["incident_context"]
    assert len(context["events"]) == 3 and len(context["groups"]) == 2
    group = next(g for g in context["groups"] if g["id"] == context["primary_group_id"])
    assert set(group["services"]) == {"database", "api"}
    assert "unrelated-sentinel" not in json.dumps(report["hypotheses"])
    assert "1 other group(s) excluded" in report["summary"]
