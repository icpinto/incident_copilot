from incident_copilot.graph.state import DiagnosisState


def finalize_report_node(state: DiagnosisState) -> DiagnosisState:
    likely_service = state.get("likely_service") or "unknown"
    likely_error_class = state.get("likely_error_class") or "unknown"
    count = len(state.get("candidates", []))
    state["summary"] = (
        f"The incident was localized to {count} strong candidate locations. "
        f"Most likely service: {likely_service}. "
        f"Most likely error class: {likely_error_class}."
    )
    return state
