"""Shared bounded initial observations for both diagnosis modes."""
import hashlib
import json

from incident_copilot.correlation.log_parser import group_events
from incident_copilot.schemas.investigation import EvidenceItem
from incident_copilot.schemas.logs import IncidentContext

MAX_EVIDENCE_BYTES = 65536


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()[:24]


def initial_evidence(state: dict, snapshot: dict, max_bytes: int = MAX_EVIDENCE_BYTES) -> tuple[dict, int]:
    parsed = state.get("parsed_log", {})
    identity = {key: snapshot[key] for key in ("repository", "revision")}
    records = {}
    omitted = 0
    initial_bytes = 0
    def add(kind, data=None, content="", **location):
        nonlocal omitted, initial_bytes
        item = EvidenceItem(id="e-" + digest([identity, kind, data, content, location]), kind=kind,
                            repository=snapshot["repository"], revision=snapshot["revision"],
                            data=data or {}, content=content, **location).model_dump(mode="json")
        size = len(encoded(item))
        if size <= 8192 and initial_bytes + size <= max_bytes:
            records[item["id"]] = item
            initial_bytes += size
        else:
            omitted += 1
    log = {k: [str(v)[:512] for v in parsed.get(k, [])[:10]]
           for k in ("services", "exception_types", "error_lines", "keywords")}
    add("log", log)
    if state.get("incident_context"):
        context = IncidentContext.model_validate(state["incident_context"])
        for event in group_events(context, context.primary_group_id):
            item = event.model_dump(exclude={"exceptions", "stack_frames"})
            clipped = len(item["message"]) > 2000
            item["message"] = item["message"][:2000]
            if clipped:
                item["limitations"] = item["limitations"] + ["Event message clipped for model evidence."]
            add("log_event", item, truncated=clipped)
            for exception in event.exceptions:
                data = exception.model_dump()
                data.update(event_id=event.id, group_id=context.primary_group_id)
                add("exception_chain", data)
    for candidate in state.get("candidates", [])[:10]:
        snippet = candidate.get("snippet") or ""
        clipped = len(snippet.splitlines()) > 80 or len(snippet.encode()) > 4096
        add("candidate", {k: candidate.get(k) for k in ("symbol_name", "score", "reasons")},
            content="\n".join(snippet.splitlines()[:80]).encode()[:4096].decode("utf-8", "ignore"),
            truncated=clipped, limitations=["Candidate snippet clipped."] if clipped else [],
            file_path=candidate.get("file_path"), line_start=candidate.get("line_start"), line_end=candidate.get("line_end"))
    for path, analysis in state.get("deep_analysis", {}).items():
        add("file_analysis", analysis, file_path=path)
    if state.get("graph_context"):
        add("code_graph", state["graph_context"])
    if state.get("runtime_config"):
        config = state["runtime_config"]
        add("runtime_config", {"status": config.get("status"),
            "environment": config.get("environment", [])[:32],
            "mounts": config.get("mounts", [])[:16],
            "truncated": bool(config.get("truncated") or len(config.get("environment", [])) > 32
                              or len(config.get("mounts", [])) > 16)},
            limitations=["Collector observation only; redacted values and mount contents are unavailable."])
    for raw in state.get("runtime_evidence", []):
        item = EvidenceItem.model_validate(raw).model_dump(mode="json")
        size = len(encoded(item))
        if size <= 8192 and initial_bytes + size <= max_bytes:
            records[item["id"]] = item
            initial_bytes += size
        else:
            omitted += 1
    return records, omitted
