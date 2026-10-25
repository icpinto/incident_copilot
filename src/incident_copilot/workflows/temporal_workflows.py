from datetime import timedelta
from uuid import uuid4

try:
    from temporalio import workflow  # type: ignore
except Exception:  # pragma: no cover
    class DummyWorkflow:
        def defn(self, cls): return cls
        def run(self, fn): return fn
        def query(self, fn): return fn
        def patched(self, _): return True
        def uuid4(self): return uuid4()
        async def execute_activity(self, fn, *args, **kwargs): raise RuntimeError("Temporal not installed")
    workflow = DummyWorkflow()

with workflow.unsafe.imports_passed_through() if hasattr(workflow, "unsafe") else __import__("contextlib").nullcontext():
    from incident_copilot.dashboard.activities import (
        dashboard_stage_activity,
        validate_repository_activity,
    )
    from incident_copilot.schemas.incident import IncidentInput
    from incident_copilot.workflows.temporal_activities import (
        build_service_map_and_index_activity,
        correlate_logs_activity,
        deep_analysis_activity,
        diagnosis_graph_activity,
    )


@workflow.defn
class ValidateRepositoryWorkflow:
    @workflow.run
    async def run(self, profile: dict) -> dict:
        return await workflow.execute_activity(
            validate_repository_activity, profile, start_to_close_timeout=timedelta(seconds=20),
        )


@workflow.defn
class DiagnoseIncidentWorkflow:
    def __init__(self):
        self.progress = {"stage": "queued", "timeline": []}

    @workflow.query
    def get_progress(self) -> dict:
        return self.progress

    @workflow.run
    async def run(self, incident: dict) -> dict:
        versioned = workflow.patched("bounded-investigation-v1")
        reasoning_options = {"heartbeat_timeout": timedelta(seconds=20)} if versioned else {}
        if "dashboard" in incident:
            from temporalio.common import RetryPolicy
            incident_id = incident["dashboard"]["incident_id"]
            for stage in ("indexing", "correlating", "analyzing", "reasoning"):
                self.progress["stage"] = stage
                event = await workflow.execute_activity(
                    dashboard_stage_activity, args=[incident_id, stage],
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                    **(reasoning_options if stage == "reasoning" else {}),
                )
                self.progress["timeline"].append(event)
            self.progress["stage"] = "completed"
            return {"incident_id": incident_id}
        inc = IncidentInput.model_validate(incident)
        incident_id = inc.incident_id or str(workflow.uuid4() if versioned else uuid4())
        # Context is appended only for patched histories. Repository identity and
        # the execution-specific index stay outside model-selected tool arguments.
        context = {"repo_root": str(inc.repo_root), "execution_id": str(workflow.uuid4())} if versioned else None
        appended = [None, context] if versioned else []

        await workflow.execute_activity(
            build_service_map_and_index_activity,
            args=[str(inc.repo_root), str(inc.compose_path) if inc.compose_path else None, *appended],
            start_to_close_timeout=timedelta(minutes=10),
        )
        correlation = await workflow.execute_activity(
            correlate_logs_activity,
            args=[inc.model_dump(mode="json"), *appended],
            start_to_close_timeout=timedelta(minutes=5),
        )
        deep_analysis = await workflow.execute_activity(
            deep_analysis_activity,
            args=[str(inc.repo_root), correlation["candidates"], *appended],
            start_to_close_timeout=timedelta(minutes=5),
        )
        report = await workflow.execute_activity(
            diagnosis_graph_activity,
            args=[incident_id, correlation["parsed_log"], correlation["candidates"], deep_analysis, *appended],
            start_to_close_timeout=timedelta(minutes=10),
            **reasoning_options,
        )
        return report
