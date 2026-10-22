"""Opt-in real-server test: IC_RUN_TEMPORAL_TESTS=1 python -m pytest tests/test_temporal_integration.py -q."""
import asyncio
import os
from datetime import timedelta

import pytest

pytestmark = pytest.mark.skipif(os.getenv("IC_RUN_TEMPORAL_TESTS") != "1", reason="Requires downloading/running the Temporal dev server")


def test_real_temporal_recovery_queries_validation_and_exports(tmp_path, monkeypatch):
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from incident_copilot.dashboard.activities import dashboard_stage_activity, validate_repository_activity
    from incident_copilot.dashboard.models import IncidentSubmission, SettingsUpdate
    from incident_copilot.dashboard.service import DashboardService
    from incident_copilot.dashboard.store import ControlStore, utcnow
    from incident_copilot.workflows.temporal_workflows import DiagnoseIncidentWorkflow, ValidateRepositoryWorkflow
    from incident_copilot.graph.nodes import build_hypotheses

    monkeypatch.setenv("IC_CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("IC_DATA_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IC_INDEX_DB_PATH", str(tmp_path / "indexes" / "repo.db"))
    class NoModel:
        async def complete(self, **kwargs):
            await asyncio.sleep(.2)
            raise ConnectionError("offline")
    monkeypatch.setattr(build_hypotheses, "get_client", lambda *_: NoModel())

    async def scenario():
        download_dir = tmp_path / "temporal"
        download_dir.mkdir()
        async with await WorkflowEnvironment.start_local(dev_server_log_level="error", download_dest_dir=str(download_dir)) as environment:
            monkeypatch.setenv("IC_TEMPORAL_HOST", environment.client.service_client.config.target_host)
            service = DashboardService(ControlStore(tmp_path / "control.db"))
            service.configuration.save(SettingsUpdate(values={"openai_compatible_api_key": "integration-secret-key"}))
            root = tmp_path / "source"
            root.mkdir()
            (root / "main.py").write_text("def charge():\n    raise ValueError('payment failed')\n")
            service.store.put("repositories", "repo", {"id": "repo", "name": "Test repository", "repo_root": str(root), "compose_path": None, "archived": False, "created_at": utcnow()})
            async with Worker(environment.client, task_queue=service.configuration.effective().temporal_task_queue,
                workflows=[DiagnoseIncidentWorkflow, ValidateRepositoryWorkflow],
                activities=[dashboard_stage_activity, validate_repository_activity]):
                validation = await service.gateway.validate({"repo_root": str(root), "compose_path": None})
                assert validation["valid"]
                run = service.submit(IncidentSubmission(repository_id="repo", log_text='api | ValueError: payment failed\nFile "/app/main.py", line 2, in charge'))
                await service.reconcile()
                # Simulate API restart and a lost start acknowledgement.
                service.store.update("incidents", run.id, {"dispatched": False})
                restarted = DashboardService(service.store)
                for _ in range(60):
                    await restarted.reconcile()
                    saved = service.store.get("incidents", run.id)
                    if saved["status"] in {"completed", "failed"}:
                        break
                    await asyncio.sleep(.25)
                assert saved["status"] == "completed", saved
                assert saved["report"]["engine"] == "deterministic"
                assert len(saved["timeline"]) == 4
                handle = environment.client.get_workflow_handle(f"incident-{run.id}")
                progress = await handle.query("get_progress")
                assert progress["stage"] == "completed"
                assert "integration-secret-key" not in str((await handle.fetch_history()).events)
                assert (tmp_path / "artifacts" / "incidents" / run.id / "report.md").exists()

    asyncio.run(scenario())
