from __future__ import annotations

import asyncio
import json
from pathlib import Path

from incident_copilot.core.settings import Settings
from incident_copilot.correlation.candidate_ranker import rank_candidates
from incident_copilot.analysis.dependency_probe import deep_analyze_candidate
from incident_copilot.analysis.code_graph import retrieve_graph_context
from incident_copilot.graph.diagnosis_graph import run_diagnosis_graph
from incident_copilot.indexing.repo_indexer import index_repository
from incident_copilot.indexing.service_mapper import build_service_map
from incident_copilot.reporting.report_builder import build_report
from incident_copilot.reporting.markdown_renderer import render_markdown
from incident_copilot.schemas.diagnosis import DiagnosisReport
from .config import Configuration, control_path
from .store import ControlStore, utcnow

try:
    from temporalio import activity
except ImportError:  # Allows deterministic analysis without the Temporal extra.
    class _Activity:
        def defn(self, fn):
            return fn
    activity = _Activity()


def validate_repository(repo_root: str, compose_path: str | None) -> dict:
    root = Path(repo_root).expanduser()
    if not root.is_absolute() or not root.is_dir():
        return {"valid": False, "message": "Repository must be an existing absolute directory on the worker.", "services": []}
    compose = Path(compose_path).expanduser() if compose_path else None
    if compose and not compose.is_absolute():
        compose = root / compose
    if compose and not compose.is_file():
        return {"valid": False, "message": "Compose file does not exist on the worker.", "services": []}
    try:
        services = [item.model_dump() for item in build_service_map(root, compose)]
    except Exception:
        return {"valid": False, "message": "Compose file could not be parsed. Check its YAML and service definitions.", "services": []}
    return {"valid": True, "message": "Repository is accessible from the worker.", "services": services}


@activity.defn
async def validate_repository_activity(profile: dict) -> dict:
    return await asyncio.to_thread(validate_repository, profile["repo_root"], profile.get("compose_path"))


@activity.defn
async def dashboard_stage_activity(incident_id: str, stage: str) -> dict:
    """Secrets and evidence remain in the shared store, outside workflow histories."""
    store = ControlStore(control_path())
    run = store.get("incidents", incident_id)
    if run is None:
        raise ValueError("Incident not found in shared control store")
    config = Settings(**store.get("revisions", run["config_revision"]))
    inputs = run["input"]
    root = Path(inputs["repo_root"])
    compose = Path(inputs["compose_path"]) if inputs.get("compose_path") else None
    if compose and not compose.is_absolute():
        compose = root / compose
    evidence = run["evidence"]
    timeline = run["timeline"]
    try:
        attempt = activity.info().attempt
    except (AttributeError, RuntimeError):
        attempt = 1
    event = {"stage": stage, "started_at": utcnow(), "completed_at": None, "attempt": attempt}
    timeline = [item for item in timeline if item["stage"] != stage] + [event]
    store.update("incidents", incident_id, {
        "status": "running", "stage": stage, "timeline": timeline, "updated_at": utcnow(),
        "connection_error": None,
    })
    changes = {}
    if stage == "indexing":
        validation = await asyncio.to_thread(validate_repository, str(root), str(compose) if compose else None)
        if not validation["valid"]:
            raise ValueError(validation["message"])
        summary = await asyncio.to_thread(index_repository, root, compose, Path(run["index_db_path"]))
        evidence["index_summary"] = summary.model_dump(mode="json")
        if store.get("repositories", run["repository_id"]):
            store.update("repositories", run["repository_id"], {"index_summary": evidence["index_summary"], "validation": validation})
    elif stage == "correlating":
        candidates, parsed = await asyncio.to_thread(
            rank_candidates, root, Path(run["index_db_path"]), inputs["log_text"],
            config.top_k_candidates, inputs.get("user_hint_service"),
        )
        evidence["parsed_log"] = parsed.model_dump()
        evidence["candidates"] = [item.model_dump() for item in candidates.items]
    elif stage == "analyzing":
        results = {}
        for candidate in evidence.get("candidates", [])[:config.deep_analysis_top_n]:
            path = root / candidate["file_path"]
            if path.is_file():
                results[candidate["file_path"]] = await asyncio.to_thread(deep_analyze_candidate, path)
        evidence["deep_analysis"] = results
        if "graph_context" not in evidence:
            evidence["graph_context"] = await retrieve_graph_context(
                root, Path(run["index_db_path"]), evidence.get("candidates", []), config,
            )
    elif stage == "reasoning":
        state = await run_diagnosis_graph({
            "incident_id": incident_id, "parsed_log": evidence.get("parsed_log", {}),
            "candidates": evidence.get("candidates", []), "deep_analysis": evidence.get("deep_analysis", {}),
            "candidate_limit": config.top_k_candidates,
            "graph_context": evidence.get("graph_context"),
        }, config)
        report = build_report(state).model_dump(mode="json")
        # Redact at persistence time as well as at the HTTP boundary.
        changes["report"] = Configuration(store, activate=False).redact(report)
        artifact_dir = Path(run["artifact_dir"])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "report.json").write_text(json.dumps(changes["report"], indent=2), encoding="utf-8")
        (artifact_dir / "report.md").write_text(render_markdown(DiagnosisReport.model_validate(changes["report"])), encoding="utf-8")
    else:
        raise ValueError("Unknown analysis stage")
    event["completed_at"] = utcnow()
    changes.update({"evidence": evidence, "timeline": timeline, "updated_at": utcnow()})
    store.update("incidents", incident_id, changes)
    return event
