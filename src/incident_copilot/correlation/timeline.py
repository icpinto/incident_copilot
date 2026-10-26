"""Deterministic log records and conservative correlation, independent of the model."""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import UTC, datetime

from incident_copilot.schemas.logs import (
    IncidentContext,
    IncidentGroup,
    LogEvent,
    LogException,
)

from .stacktrace_parser import parse_stack_frames

SERVICE = re.compile(r"^\s*([\w.-]+)\s+\|\s?(.*)$")
STAMP = re.compile(r"^\s*\[?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?|\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\]?\s*")
IDENTIFIER = re.compile(r'\b(trace_id|traceId|request_id|requestId)\s*[=:]\s*["\']?([\w.:-]+)')
EXCEPTION = re.compile(r"(?:^|\s|\[cause\]:\s*)([\w.$]*(?:Error|Exception)|panic)\b(?::\s*(.*))?")
ERROR = re.compile(r"\b(error|exception|traceback|failed|panic|fatal)\b", re.IGNORECASE)
CHAIN = re.compile(r"The above exception was the direct cause|During handling of the above exception|Caused by:|\[cause\]:")
FRAME = re.compile(r'^\s*(?:File "|at\s|\.\.\.\s*\d+|\^|Traceback\b)')
LOG_LEVEL = re.compile(r"^\s*(?:ERROR|FATAL|CRITICAL|WARN(?:ING)?|INFO|DEBUG)\b")


def _exception_header(message: str):
    if FRAME.match(message) or message.lstrip().startswith(("raise ", "throw ")):
        return None
    return EXCEPTION.search(message)


def _id(prefix: str, value: str) -> str:
    return prefix + hashlib.sha256(value.encode()).hexdigest()[:16]


def _utc(stamp: str | None) -> str | None:
    if not stamp or not re.search(r"(?:Z|[+-]\d{2}:?\d{2})$", stamp):
        return None
    try:
        value = datetime.fromisoformat(stamp.replace(",", "."))
        if value.tzinfo is not None:
            return value.astimezone(UTC).isoformat()
    except ValueError:
        pass
    return None


def _text_metadata(text: str) -> tuple[str, dict]:
    fields = {}
    prefix = SERVICE.match(text)
    if prefix:
        fields["service"] = prefix[1]
        text = prefix[2]
    stamp = STAMP.match(text)
    if stamp:
        fields["timestamp"] = stamp[1]
        text = text[stamp.end():]
    for match in IDENTIFIER.finditer(text):
        key = "trace_id" if match[1] in {"trace_id", "traceId"} else "request_id"
        fields[key] = match[2]
    return text, fields


def _record(line: str) -> tuple[str, dict, list[str], bool]:
    message, fields = _text_metadata(line)
    limitations = []
    structured = False
    if message.lstrip().startswith("{"):
        try:
            data = json.loads(message)
        except (ValueError, RecursionError):
            limitations.append("Malformed JSON record retained as text.")
        else:
            if isinstance(data, dict):
                structured = True
                body = next((data[name] for name in ("message", "msg")
                             if isinstance(data.get(name), str)), None)
                if isinstance(body, str):
                    message, embedded = _text_metadata(body)
                    for key, value in embedded.items():
                        if key in fields and fields[key] != value:
                            limitations.append(f"Conflicting {key} metadata; outer prefix retained.")
                        else:
                            fields[key] = value
                aliases = {
                    "timestamp": ("timestamp", "@timestamp", "time"),
                    "service": ("service", "service_name", "service.name"),
                    "trace_id": ("trace_id", "traceId"),
                    "request_id": ("request_id", "requestId"),
                }
                for key, names in aliases.items():
                    values = []
                    for name in names:
                        value = data.get(name)
                        if name == "service" and isinstance(value, dict):
                            value = value.get("name")
                        if isinstance(value, str) and value.strip():
                            values.append(value.strip())
                    if values:
                        if len(set(values)) > 1 or (key in fields and fields[key] != values[0]):
                            limitations.append(f"Conflicting {key} metadata; structured field retained.")
                        fields[key] = values[0]
                level = data.get("level", data.get("severity", ""))
                if isinstance(level, str) and level.lower() in {"error", "fatal", "critical"}:
                    fields["error_bearing"] = True
    return message, fields, limitations, structured


def _exceptions(event: LogEvent) -> list[LogException]:
    """Python frames precede their exception; JVM/Node frames follow it."""
    result = []
    pending_frames = []
    relation = None
    python_trace = False
    for line in event.message.splitlines():
        if "Traceback" in line or line.lstrip().startswith('File "'):
            python_trace = True
        if "The above exception was the direct cause" in line:
            relation = "caused_by"
        elif "During handling of the above exception" in line:
            relation = "during_handling"
        elif "Caused by:" in line or "[cause]:" in line:
            relation = "cause"
        frames = parse_stack_frames(line)
        # A source-code line can contain an exception constructor; it is not a header.
        match = _exception_header(line)
        if match:
            current = LogException(
                id=f"{event.id}-x{len(result) + 1}", type=match[1], message=match[2] or "",
                relation=relation if result else None,
                previous_id=result[-1].id if result and relation else None,
                stack_frames=pending_frames if python_trace else [],
            )
            result.append(current)
            pending_frames = []
            relation = None
        elif frames:
            if result and not python_trace:
                result[-1].stack_frames.extend(frames)
            else:
                pending_frames.extend(frames)
    return result


def parse_incident_context(text: str) -> IncidentContext:
    # Builders remain open per explicit stream so interleaved prefixed traces survive.
    builders: list[dict] = []
    active: dict[tuple, dict] = {}
    for number, line in enumerate(text.splitlines(), 1):
        message, fields, limitations, structured = _record(line)
        if not message.strip() and not active:
            continue
        stream = (fields.get("service"), fields.get("trace_id"), fields.get("request_id"))
        identified = any(stream)
        builder = active.get(stream) if identified else None
        if identified and builder is None:
            compatible = [b for key, b in active.items() if all(
                value is None or key[i] == value for i, value in enumerate(stream)
            )]
            if len(compatible) == 1:
                builder = compatible[0]
        if not identified:
            possible = list({id(b): b for b in active.values()}.values())
            builder = possible[0] if len(possible) == 1 else None
            if len(possible) > 1 and (FRAME.match(message) or EXCEPTION.search(message) or CHAIN.search(message)):
                limitations.append("Ambiguous continuation retained without service ownership.")
        chain = bool(CHAIN.search(message))
        header = bool(_exception_header(message))
        trace_start = message.lstrip().startswith("Traceback")
        continuation = bool(builder) and (
            chain or (FRAME.match(message) and (not trace_start or not builder["terminal"] or builder["chain"])) or not message.strip()
            or (header and (not builder["terminal"] or builder["chain"]))
            or (message[:1].isspace() and not header and not LOG_LEVEL.match(message))
        )
        # A JSON line is a complete record; its message may itself contain a full trace.
        if structured:
            continuation = False
        if continuation:
            builder["parts"].append(message)
            builder["end"] = number
            builder["limitations"].extend(limitations)
            builder["chain"] = (chain or builder["chain"]) and not header
            if header:
                builder["terminal"] = True
            continue
        # A new independent record closes only its own stream.
        active.pop(stream, None)
        if builder:
            active = {key: value for key, value in active.items() if value is not builder}
        builder = {"start": number, "end": number, "parts": [message], "fields": fields,
                   "limitations": limitations, "terminal": header, "chain": chain}
        builders.append(builder)
        if not structured and (FRAME.match(message) or header or ERROR.search(message)):
            active[stream] = builder

    events = []
    for builder in builders:
        message = "\n".join(builder["parts"])
        fields = builder["fields"]
        event = LogEvent(
            id=_id("ev-", f'{builder["start"]}:{message}'),
            line_start=builder["start"], line_end=builder["end"], message=message,
            **{k: v for k, v in fields.items() if k != "error_bearing"},
            timestamp_utc=_utc(fields.get("timestamp")),
            error_bearing=bool(fields.get("error_bearing") or ERROR.search(message) or EXCEPTION.search(message)),
            stack_frames=parse_stack_frames(message), limitations=builder["limitations"],
        )
        event.exceptions = _exceptions(event)
        if event.timestamp and not event.timestamp_utc:
            event.limitations.append("Timestamp is invalid or lacks an explicit date and timezone; source order retained.")
        events.append(event)
    return _group(events)


def _group(events: list[LogEvent]) -> IncidentContext:
    traces_for_request: dict[str, set[str]] = defaultdict(set)
    for event in events:
        if event.request_id and event.trace_id:
            traces_for_request[event.request_id].add(event.trace_id)
    grouped: dict[tuple, list[LogEvent]] = {}
    limitations = []
    for event in events:
        traces = traces_for_request[event.request_id] if event.request_id else set()
        if len(traces) > 1:
            limitation = f"Request ID {event.request_id} spans conflicting traces; request-only association disabled."
            event.limitations.append(limitation)
            limitations.append(limitation)
        if event.trace_id:
            key = ("trace_id", event.trace_id)
        elif event.request_id and len(traces) == 1:
            key = ("trace_id", next(iter(traces)))
        elif event.request_id and not traces:
            key = ("request_id", event.request_id)
        else:
            key = ("record", event.id)
        grouped.setdefault(key, []).append(event)
    groups = [IncidentGroup(
        id=_id("grp-", ":".join(key)), event_ids=[e.id for e in members],
        services=list(dict.fromkeys(e.service for e in members if e.service)),
        error_bearing=any(e.error_bearing for e in members), first_line=members[0].line_start,
        grouping_basis=key[0],
    ) for key, members in grouped.items()]
    return IncidentContext(events=events, groups=groups, limitations=list(dict.fromkeys(limitations)))
