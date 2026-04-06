from incident_copilot.graph.state import DiagnosisState


def select_candidates_node(state: DiagnosisState) -> DiagnosisState:
    candidates = state.get("candidates", [])
    state["candidates"] = sorted(candidates, key=lambda x: x.get("score", 0), reverse=True)[:5]
    return state
