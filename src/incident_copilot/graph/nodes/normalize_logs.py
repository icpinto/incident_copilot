from incident_copilot.graph.state import DiagnosisState


def normalize_logs_node(state: DiagnosisState) -> DiagnosisState:
    parsed = state.get("parsed_log", {})
    services = parsed.get("services", [])
    exception_types = parsed.get("exception_types", [])
    state["likely_service"] = services[0] if services else None
    state["likely_error_class"] = exception_types[0] if exception_types else None
    return state
