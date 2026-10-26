"""Immutable, content-addressed log snapshots kept outside Temporal history."""
import hashlib
import json
import re

from incident_copilot.dashboard.config import control_path
from incident_copilot.dashboard.store import ControlStore


def offload_parsed_log(parsed: dict) -> dict:
    serialized = json.dumps(parsed, sort_keys=True, ensure_ascii=False)
    key = "incident-context-" + hashlib.sha256(serialized.encode()).hexdigest()
    store = ControlStore(control_path())
    store.put("revisions", key, parsed)
    return {"incident_context_ref": key}


def hydrate_parsed_log(parsed: dict) -> dict:
    key = parsed.get("incident_context_ref")
    if key is None:
        return parsed
    if not isinstance(key, str) or not re.fullmatch(r"incident-context-[a-f0-9]{64}", key):
        raise ValueError("Invalid incident context reference")
    snapshot = ControlStore(control_path()).get("revisions", key)
    if snapshot is None:
        raise ValueError("Incident context snapshot is unavailable")
    serialized = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
    if "incident-context-" + hashlib.sha256(serialized.encode()).hexdigest() != key:
        raise ValueError("Incident context snapshot identity mismatch")
    return snapshot
