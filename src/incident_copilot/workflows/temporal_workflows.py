from datetime import timedelta
from uuid import uuid4

try:
    from temporalio import workflow  # type: ignore
except Exception:  # pragma: no cover
    class DummyWorkflow:
        def defn(self, cls): return cls
        def run(self, fn): return fn
        async def execute_activity(self, fn, *args, **kwargs): raise RuntimeError("Temporal not installed")
    workflow = DummyWorkflow()

with workflow.unsafe.imports_passed_through() if hasattr(workflow, "unsafe") else __import__("contextlib").nullcontext():
    from incident_copilot.schemas.incident import IncidentInput
    from incident_copilot.workflows.temporal_activities import (
        build_service_map_and_index_activity,
        correlate_logs_activity,
        deep_analysis_activity,
        diagnosis_graph_activity,
    )


@workflow.defn
class DiagnoseIncidentWorkflow:
    @workflow.run
    async def run(self, incident: dict) -> dict:
        inc = IncidentInput.model_validate(incident)
        incident_id = inc.incident_id or str(uuid4())

        await workflow.execute_activity(
            build_service_map_and_index_activity,
            args=[str(inc.repo_root), str(inc.compose_path) if inc.compose_path else None],
            start_to_close_timeout=timedelta(minutes=10),
        )
        correlation = await workflow.execute_activity(
            correlate_logs_activity,
            args=[inc.model_dump(mode="json")],
            start_to_close_timeout=timedelta(minutes=5),
        )
        deep_analysis = await workflow.execute_activity(
            deep_analysis_activity,
            args=[str(inc.repo_root), correlation["candidates"]],
            start_to_close_timeout=timedelta(minutes=5),
        )
        report = await workflow.execute_activity(
            diagnosis_graph_activity,
            args=[incident_id, correlation["parsed_log"], correlation["candidates"], deep_analysis],
            start_to_close_timeout=timedelta(minutes=10),
        )
        return report
