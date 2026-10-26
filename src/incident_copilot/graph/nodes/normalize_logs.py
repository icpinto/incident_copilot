from incident_copilot.correlation.log_parser import group_events, primary_log
from incident_copilot.graph.state import DiagnosisState
from incident_copilot.schemas.logs import IncidentContext


def normalize_logs_node(state: DiagnosisState) -> DiagnosisState:
    parsed = state.get("parsed_log", {})
    context = parsed.get("incident_context") or state.get("incident_context")
    if context and context.get("primary_group_id"):
        state["incident_context"] = context
        parsed = primary_log(parsed)
        state["parsed_log"] = parsed
        services = parsed.get("services", [])
        top = next(iter(state.get("candidates", [])), {})
        service = top.get("service_name")
        state["likely_service"] = service if service in services else services[0] if len(services) == 1 else None
        events = group_events(IncidentContext.model_validate(context), context["primary_group_id"])
        from incident_copilot.correlation.locations import stack_path_match
        matches = [(stack_path_match(top.get("file_path", ""), frame.file_path),
                    bool(frame.line_number and top.get("line_start") and top.get("line_end")
                         and top["line_start"] <= frame.line_number <= top["line_end"]), exception)
                   for event in events for exception in event.exceptions for frame in exception.stack_frames]
        matched = max(reversed(matches), key=lambda item: (item[0], item[1]), default=None)
        terminal = next((e.exceptions[-1].type for e in reversed(events) if e.exceptions), None)
        state["likely_error_class"] = matched[2].type if matched and matched[0][0] else terminal
        return state
    services = parsed.get("services", [])
    exception_types = parsed.get("exception_types", [])
    state["likely_service"] = services[0] if services else None
    state["likely_error_class"] = exception_types[0] if exception_types else None
    return state
