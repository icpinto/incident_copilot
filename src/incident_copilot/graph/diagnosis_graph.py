import asyncio
from typing import Any

from incident_copilot.analysis.runbooks import load_runbooks
from incident_copilot.core.settings import settings
from incident_copilot.graph.investigation_runtime import SourceSnapshotUnverifiedError
from incident_copilot.graph.nodes.enrich_candidates import enrich_candidates_node
from incident_copilot.graph.nodes.finalize_report import finalize_report_node
from incident_copilot.graph.nodes.normalize_logs import normalize_logs_node
from incident_copilot.graph.nodes.select_candidates import select_candidates_node
from incident_copilot.observability import observed
from incident_copilot.tools.git_local import clean_revision

try:
    from langgraph.graph import END, StateGraph  # type: ignore
except Exception:  # noqa: BLE001 -- Optional graph runtime. # pragma: no cover
    END = "__end__"
    StateGraph = None

from .nodes.build_hypotheses import build_hypotheses_node
from .state import DiagnosisState


@observed("diagnosis.pipeline")
async def run_diagnosis_graph(payload: dict[str, Any], runtime_settings=None, runtime_context=None) -> DiagnosisState:
    payload = dict(payload)
    payload["action_plan_version"] = 1
    if "runbooks" not in payload:
        root = runtime_context.repo_root if runtime_context and not payload.get("source_unavailable") else None
        revision = await asyncio.to_thread(clean_revision, root) if root is not None else None
        parsed = payload.get("parsed_log", {})
        from incident_copilot.correlation.log_parser import primary_log
        parsed = primary_log(parsed)
        payload["runbooks"], payload["recommendation_limitations"] = await asyncio.to_thread(
            load_runbooks, root, revision, parsed.get("services", []), parsed.get("exception_types", []),
            runtime_settings or settings,
        )
        payload["source_snapshot"] = {"repository": str(root.resolve()) if root else "", "revision": revision or ""}
    if runtime_settings is not None and getattr(runtime_settings, "agentic_enabled", False):
        reason = None
        if runtime_context is None:
            reason = "investigation_context_missing"
        else:
            try:
                from .investigation import run_investigation
                return await run_investigation(payload, runtime_settings, runtime_context)
            except ImportError:
                reason = "agentic_dependency_unavailable"
            except SourceSnapshotUnverifiedError:
                reason = "source_snapshot_unverified"
        # Preserve the established single-pass path and explicitly report the downgrade.
        fallback_settings = runtime_settings.model_copy(update={"agentic_enabled": False})
        state = await run_diagnosis_graph(payload, fallback_settings, runtime_context)
        from incident_copilot.schemas.investigation import InvestigationSummary
        state["investigation"] = InvestigationSummary(
            stop_reason=reason, incomplete=True, model_calls=int(bool(state.get("candidates"))),
        ).model_dump(mode="json")
        return state
    async def hypotheses_node(state):
        return await build_hypotheses_node(state, runtime_settings, runtime_context)

    if StateGraph is None:
        state: DiagnosisState = payload  # fallback
        state = normalize_logs_node(state)
        state = select_candidates_node(state)
        state = enrich_candidates_node(state)
        state = await hypotheses_node(state)
        state = finalize_report_node(state)
        return state

    workflow = StateGraph(DiagnosisState)
    workflow.add_node("normalize_logs", normalize_logs_node)
    workflow.add_node("select_candidates", select_candidates_node)
    workflow.add_node("enrich_candidates", enrich_candidates_node)
    workflow.add_node("build_hypotheses", hypotheses_node)
    workflow.add_node("finalize_report", finalize_report_node)

    workflow.set_entry_point("normalize_logs")
    workflow.add_edge("normalize_logs", "select_candidates")
    workflow.add_edge("select_candidates", "enrich_candidates")
    workflow.add_edge("enrich_candidates", "build_hypotheses")
    workflow.add_edge("build_hypotheses", "finalize_report")
    workflow.add_edge("finalize_report", END)
    app = workflow.compile()
    result = await app.ainvoke(payload)
    return result
