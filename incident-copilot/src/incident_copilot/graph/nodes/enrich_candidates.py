from incident_copilot.graph.state import DiagnosisState


def enrich_candidates_node(state: DiagnosisState) -> DiagnosisState:
    candidates = state.get("candidates", [])
    for c in candidates:
        c["evidence_summary"] = "; ".join(c.get("reasons", [])[:5])
    state["candidates"] = candidates
    return state
