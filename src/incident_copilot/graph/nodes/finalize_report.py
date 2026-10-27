from incident_copilot.graph.state import DiagnosisState


def finalize_report_node(state: DiagnosisState) -> DiagnosisState:
    likely_service = state.get("likely_service") or "unknown"
    likely_error_class = state.get("likely_error_class") or "unknown"
    count = len(state.get("candidates", []))
    state["summary"] = (
        f"Source correlation returned {count} candidate locations. "
        f"Diagnosis outcome: {state.get('diagnosis_status', 'unknown').replace('_', ' ')}. "
        f"Most likely service: {likely_service}. "
        f"Most likely error class: {likely_error_class}."
    )
    if state.get("incident_context"):
        from incident_copilot.correlation.log_parser import scope_notice
        state["summary"] += " " + scope_notice(state["incident_context"])
    return state
