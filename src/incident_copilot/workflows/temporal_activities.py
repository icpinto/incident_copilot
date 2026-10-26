import asyncio
import re
from pathlib import Path

from incident_copilot.analysis.code_graph import retrieve_graph_context
from incident_copilot.analysis.dependency_probe import deep_analyze_candidate
from incident_copilot.core.settings import Settings
from incident_copilot.correlation.candidate_ranker import rank_candidates
from incident_copilot.graph.diagnosis_graph import run_diagnosis_graph
from incident_copilot.indexing.repo_indexer import index_repository
from incident_copilot.reporting.report_builder import build_report
from incident_copilot.schemas.incident import IncidentInput
from incident_copilot.workflows.activity_heartbeat import reasoning_heartbeat
from incident_copilot.workflows.log_context import hydrate_parsed_log, offload_parsed_log

try:
    from temporalio import activity  # type: ignore
except ImportError:  # pragma: no cover
    class DummyActivity:
        def defn(self, fn): return fn
    activity = DummyActivity()


def runtime_settings(runtime=None, context: dict | None = None):
    execution_id = context.get("execution_id") if context else None
    if execution_id is not None and (
        not isinstance(execution_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", execution_id)
    ):
        raise ValueError("Invalid investigation execution identifier")
    if runtime is not None:
        settings = Settings(**runtime)
    else:
        from incident_copilot.dashboard.config import Configuration, control_path
        from incident_copilot.dashboard.store import ControlStore
        store = ControlStore(control_path())
        key = f"legacy-execution-{execution_id}" if execution_id else None
        captured = store.get("revisions", key) if key else None
        settings = Settings(**captured) if captured else Configuration(store, activate=False).effective()
        if key and captured is None:
            # The first activity captures credentials/configuration in the shared
            # store, never in the workflow's serialized arguments or result.
            store.put("revisions", key, settings.model_dump(mode="json"))
    if execution_id:
        # A retry of this execution uses the same index; other runs remain isolated.
        settings.index_db_path = settings.index_db_path.parent / "runs" / execution_id / settings.index_db_path.name
    return settings


@activity.defn
async def build_service_map_and_index_activity(repo_root: str, compose_path: str | None, runtime: dict | None = None, context: dict | None = None) -> dict:
    settings = runtime_settings(runtime, context)
    summary = await asyncio.to_thread(index_repository,
        repo_root=Path(repo_root),
        compose_path=Path(compose_path) if compose_path else None,
        db_path=settings.index_db_path,
    )
    return summary.model_dump()


@activity.defn
async def correlate_logs_activity(incident: dict, runtime: dict | None = None, context: dict | None = None) -> dict:
    settings = runtime_settings(runtime, context)
    inc = IncidentInput.model_validate(incident)
    candidates, parsed_log = await asyncio.to_thread(rank_candidates,
        repo_root=inc.repo_root,
        index_db_path=settings.index_db_path,
        log_text=inc.log_text,
        top_k=settings.top_k_candidates,
        user_hint_service=inc.user_hint_service,
    )
    parsed = parsed_log.model_dump()
    if getattr(activity, "in_activity", lambda: False)():
        parsed = offload_parsed_log(parsed)
    return {
        "parsed_log": parsed,
        "candidates": [c.model_dump() for c in candidates.items],
    }


@activity.defn
async def deep_analysis_activity(repo_root: str, candidates: list[dict], runtime: dict | None = None, context: dict | None = None) -> dict:
    settings = runtime_settings(runtime, context)
    results: dict = {}
    for candidate in candidates[: settings.deep_analysis_top_n]:
        file_path = candidate["file_path"]
        abs_path = Path(repo_root) / file_path
        if abs_path.exists():
            results[file_path] = await asyncio.to_thread(deep_analyze_candidate, abs_path)
    graph_context = await retrieve_graph_context(Path(repo_root), settings.index_db_path, candidates, settings)
    # Keep the historical files-only activity result when the feature is disabled.
    # The explicit envelope is understood by diagnosis_graph_activity on replay too.
    return {"files": results, "graph_context": graph_context} if graph_context is not None else results


@activity.defn
async def diagnosis_graph_activity(incident_id: str, parsed_log: dict, candidates: list[dict], deep_analysis: dict, runtime: dict | None = None, context: dict | None = None) -> dict:
    settings = runtime_settings(runtime, context)
    reference = parsed_log.get("incident_context_ref")
    parsed_log = hydrate_parsed_log(parsed_log)
    graph_context = deep_analysis.get("graph_context") if "files" in deep_analysis else None
    files = deep_analysis.get("files", deep_analysis)
    async with reasoning_heartbeat(activity) as heartbeat:
        runtime_context = None
        if context and context.get("repo_root"):
            from incident_copilot.graph.investigation_runtime import InvestigationRuntime
            runtime_context = InvestigationRuntime(
                repo_root=Path(context["repo_root"]).resolve(), index_db_path=settings.index_db_path,
                execution_id=context["execution_id"], heartbeat=heartbeat,
            )
        state = await run_diagnosis_graph(
            {
                "incident_id": incident_id,
                "parsed_log": parsed_log,
                "candidates": candidates,
                "deep_analysis": files,
                "graph_context": graph_context,
                "candidate_limit": settings.top_k_candidates,
            }, settings, runtime_context=runtime_context,
        )
    report = build_report(state).model_dump()
    if reference:
        report.pop("incident_context", None)
        report["incident_context_ref"] = reference
    return report
