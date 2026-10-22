from typing import Any, Dict

from incident_copilot.graph.nodes.enrich_candidates import enrich_candidates_node
from incident_copilot.graph.nodes.finalize_report import finalize_report_node
from incident_copilot.graph.nodes.normalize_logs import normalize_logs_node
from incident_copilot.graph.nodes.select_candidates import select_candidates_node

try:
    from langgraph.graph import END, StateGraph  # type: ignore
except Exception:  # pragma: no cover
    END = "__end__"
    StateGraph = None

from .nodes.build_hypotheses import build_hypotheses_node
from .state import DiagnosisState


async def run_diagnosis_graph(payload: Dict[str, Any], runtime_settings=None) -> DiagnosisState:
    async def hypotheses_node(state):
        return await build_hypotheses_node(state, runtime_settings)

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
