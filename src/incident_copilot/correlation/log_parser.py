import re

from incident_copilot.schemas.logs import IncidentContext, LogEvent, ParsedLog

from .error_signature import extract_keywords, extract_table_like_entities
from .stacktrace_parser import parse_stack_frames
from .timeline import parse_incident_context

SERVICE_RE = re.compile(r'\b([a-zA-Z0-9_-]+)\s+\|\s')
EXCEPTION_RE = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception))\b')


def _summary(log_text: str) -> ParsedLog:
    services = sorted(set(SERVICE_RE.findall(log_text)))
    exception_types = sorted(set(EXCEPTION_RE.findall(log_text)))
    stack_frames = parse_stack_frames(log_text)
    error_lines: list[str] = []
    for line in log_text.splitlines():
        lowered = line.lower()
        if any(token in lowered for token in ["error", "exception", "traceback", "failed", "panic", "fatal"]):
            error_lines.append(line.strip())
    return ParsedLog(
        services=services,
        error_lines=error_lines[:50],
        exception_types=exception_types,
        stack_frames=stack_frames,
        keywords=extract_keywords(log_text),
        tables_or_entities=extract_table_like_entities(log_text),
        raw_text=log_text,
    )


def summarize_events(events: list[LogEvent]) -> ParsedLog:
    parsed = _summary("\n".join(e.message for e in events))
    parsed.services = list(dict.fromkeys(e.service for e in events if e.service))
    parsed.stack_frames = [frame for event in events for frame in event.stack_frames]
    parsed.exception_types = list(dict.fromkeys(
        exception.type for event in events for exception in event.exceptions
    )) or parsed.exception_types
    return parsed


def group_events(context: IncidentContext, group_id: str | None) -> list[LogEvent]:
    group = next((g for g in context.groups if g.id == group_id), None)
    members = set(group.event_ids) if group else set()
    return [e for e in context.events if e.id in members]


def primary_log(parsed: dict) -> dict:
    """Project new inputs without reparsing legacy reports or checkpoints."""
    raw_context = parsed.get("incident_context")
    if not raw_context:
        return parsed
    context = IncidentContext.model_validate(raw_context)
    if not context.primary_group_id:
        return parsed
    return summarize_events(group_events(context, context.primary_group_id)).model_dump()


def scope_notice(context: dict | None) -> str:
    if not context:
        return ""
    excluded = len(context.get("groups", [])) - 1
    return (
        f"Diagnosis covers primary group {context.get('primary_group_id')}; "
        f"{max(0, excluded)} other group(s) excluded from analysis. "
        "Shared identifiers and chronology establish association, not causation."
    )


def parse_logs(log_text: str) -> ParsedLog:
    context = parse_incident_context(log_text)
    parsed = summarize_events(context.events)
    # The compatibility aggregates describe the entire submission, not just its focus.
    parsed.services = sorted(parsed.services)
    parsed.exception_types = sorted(parsed.exception_types)
    parsed.raw_text = log_text
    parsed.incident_context = context
    return parsed
