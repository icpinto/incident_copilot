from __future__ import annotations

import asyncio
import json
from pathlib import Path

from incident_copilot.analysis.code_graph import retrieve_graph_context
from incident_copilot.analysis.dependency_probe import deep_analyze_candidate
from incident_copilot.core.settings import Settings
from incident_copilot.correlation.candidate_ranker import rank_candidates
from incident_copilot.graph.diagnosis_graph import run_diagnosis_graph
from incident_copilot.indexing.repo_indexer import index_repository
from incident_copilot.indexing.service_mapper import build_service_map
from incident_copilot.reporting.markdown_renderer import render_markdown
from incident_copilot.reporting.report_builder import build_report
from incident_copilot.schemas.code_graph import GraphPreparation
from incident_copilot.schemas.deployment import analysis_configuration
from incident_copilot.schemas.diagnosis import DiagnosisReport
from incident_copilot.workflows.activity_heartbeat import reasoning_heartbeat

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
    except Exception:  # noqa: BLE001 -- Return a safe validation failure for inaccessible repository metadata.
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
    if inputs.get("version_linked") and stage == "indexing":
        from temporalio.exceptions import ApplicationError

        from .deployed_source import prepare_deployed_snapshot
        from .repository_sync import SyncFailure
        try:
            async with reasoning_heartbeat(activity):
                snapshot = await asyncio.to_thread(prepare_deployed_snapshot, inputs, config,
                                                   store.repository_token(run["repository_id"]))
            inputs.update(repo_root=snapshot["root"], compose_path=snapshot.get("compose_path"),
                          repository_snapshot=snapshot,
                          repository_source={"source_type": inputs["repository_profile"]["source_type"],
                              "remote_url": inputs["repository_profile"].get("remote_url") or inputs["repository_profile"]["repo_root"],
                              "branch": inputs["repository_profile"].get("branch") or "",
                              "commit": snapshot["revision"]})
            resolution = {"status": "matched", "requested_commit": (inputs.get("deployment_context") or {}).get("revision"),
                          "analyzed_commit": snapshot["revision"],
                          "reason": "Analyzing the commit declared by the emitting container. Runtime file contents are not verified."}
            run.update(repository_source=inputs["repository_source"], source_resolution=resolution)
            store.update("incidents", incident_id, {"input": inputs, "repository_source": run["repository_source"],
                                                    "source_resolution": resolution, "error": None})
        except (SyncFailure, ValueError, OSError) as error:
            reason = str(error) if isinstance(error, SyncFailure) else "Deployed source could not be prepared. Check repository paths, Compose metadata, and worker storage; then rerun."
            store.update("incidents", incident_id, {"source_resolution": {
                "status": "unavailable", "requested_commit": (inputs.get("deployment_context") or {}).get("revision"),
                "analyzed_commit": None, "reason": reason}, "error": reason})
            if run.get("runtime_scope") and isinstance(error, SyncFailure):
                inputs.update(source_unavailable=True, repo_root=None)
                run["source_resolution"] = {"status": "unavailable", "requested_commit":
                    (inputs.get("deployment_context") or {}).get("revision"), "analyzed_commit": None, "reason": reason}
                store.update("incidents", incident_id, {"input": inputs, "error": None})
            else:
                raise ApplicationError(reason, type="DeployedSourceUnavailable",
                                       non_retryable=not getattr(error, "transient", False)) from None
    if inputs.get("version_linked") and not inputs.get("source_unavailable") and run.get("source_resolution", {}).get("status") != "matched":
        raise ValueError("Deployed source must be resolved before source analysis.")
    root = Path(inputs["repo_root"]) if inputs.get("repo_root") else Path(run["index_db_path"]).parent
    compose = Path(inputs["compose_path"]) if inputs.get("compose_path") else None
    if compose and not compose.is_absolute():
        compose = root / compose
    if inputs.get("version_linked") and not inputs.get("source_unavailable"):
        from incident_copilot.tools.git_local import clean_revision
        if await asyncio.to_thread(clean_revision, root) != inputs["repository_snapshot"]["revision"]:
            from temporalio.exceptions import ApplicationError
            reason = "The incident's pinned deployed source changed. Source analysis stopped; rerun to prepare a clean snapshot."
            store.update("incidents", incident_id, {"source_resolution": {
                "status": "unavailable", "requested_commit": (inputs.get("deployment_context") or {}).get("revision"),
                "analyzed_commit": None, "reason": reason}, "error": reason})
            if run.get("runtime_scope"):
                inputs["source_unavailable"] = True
                run["source_resolution"] = {"status": "unavailable", "requested_commit":
                    (inputs.get("deployment_context") or {}).get("revision"), "analyzed_commit": None, "reason": reason}
                store.update("incidents", incident_id, {"input": inputs, "error": None})
            else:
                raise ApplicationError(reason, type="DeployedSourceUnavailable", non_retryable=True)
    if stage == "indexing" and not inputs.get("source_unavailable"):
        validation = await asyncio.to_thread(validate_repository, str(root), str(compose) if compose else None)
        if not validation["valid"]:
            if not run.get("runtime_scope"):
                raise ValueError(validation["message"])
            inputs.update(source_unavailable=True, repo_root=None)
            run["source_resolution"] = {"status": "unavailable", "requested_commit":
                (run.get("deployment_context") or {}).get("revision"), "analyzed_commit": None,
                "reason": validation["message"]}
            store.update("incidents", incident_id, {"input": inputs, "source_resolution": run["source_resolution"], "error": None})
    if inputs.get("source_unavailable"):
        # Source preparation is unavailable, but parsing and runtime observations remain useful.
        from incident_copilot.correlation.log_parser import parse_logs
        parsed = parse_logs(inputs["log_text"])
        context = parsed.incident_context
        if context and context.groups:
            eligible = [g for g in context.groups if g.error_bearing] or context.groups
            hinted = [g for g in eligible if inputs.get("user_hint_service") in g.services]
            context.primary_group_id = (hinted or eligible)[0].id
            context.selection_reason = "Source unavailable; selected earliest eligible observed error group."
            context.limitations.append("Matching source unavailable; runtime-only diagnosis.")
        evidence.update(parsed_log=parsed.model_dump(), candidates=[], deep_analysis={}, graph_context=None)
        if stage != "reasoning":
            event["completed_at"] = utcnow()
            store.update("incidents", incident_id, {"evidence": evidence, "timeline": timeline, "updated_at": utcnow()})
            return event
    if stage == "indexing":
        if inputs.get("repository_snapshot"):
            from .repository_sync import incident_index
            evidence["index_summary"] = await asyncio.to_thread(incident_index, inputs, Path(run["index_db_path"]))
        else:
            summary = await asyncio.to_thread(index_repository, root, compose, Path(run["index_db_path"]))
            evidence["index_summary"] = summary.model_dump(mode="json")
        if run.get("deployment_context") and not inputs.get("version_linked"):
            resolution = {"status": "unknown", "requested_commit": run["deployment_context"].get("revision"),
                          "analyzed_commit": evidence["index_summary"].get("repo_revision"),
                          "reason": "Version linking is disabled for this service; analysis uses the configured repository source."}
            changes["source_resolution"] = resolution
        if not inputs.get("repository_snapshot") and store.get("repositories", run["repository_id"]):
            store.update("repositories", run["repository_id"], {"index_summary": evidence["index_summary"], "validation": validation})
    elif stage == "correlating":
        candidates, parsed = await asyncio.to_thread(
            rank_candidates, root, Path(run["index_db_path"]), inputs["log_text"],
            config.top_k_candidates, inputs.get("user_hint_service"),
        )
        evidence["parsed_log"] = parsed.model_dump()
        # Original input is already captured once in evidence.raw_log.
        evidence["parsed_log"]["raw_text"] = ""
        evidence["candidates"] = [item.model_dump() for item in candidates.items]
    elif stage == "analyzing":
        results = {}
        for candidate in evidence.get("candidates", [])[:config.deep_analysis_top_n]:
            path = root / candidate["file_path"]
            if path.is_file():
                results[candidate["file_path"]] = await asyncio.to_thread(deep_analyze_candidate, path)
        evidence["deep_analysis"] = results
        if "graph_context" not in evidence:
            async def preparation_progress(state: GraphPreparation) -> None:
                evidence["graph_preparation"] = state.model_dump(mode="json")
                store.update("incidents", incident_id, {"evidence": evidence, "updated_at": utcnow()})

            async with reasoning_heartbeat(activity):
                evidence["graph_context"] = await retrieve_graph_context(
                    root, Path(run["index_db_path"]), evidence.get("candidates", []), config,
                    progress=preparation_progress,
                )
            # A retry can fail source validation before contacting the manager.
            # Clear any pending progress left by the interrupted attempt.
            previous = evidence.get("graph_preparation") or {}
            if previous.get("status") in {"queued", "preparing"}:
                context = evidence.get("graph_context") or {}
                await preparation_progress(GraphPreparation(
                    repository=str(root), status="unavailable", reason=context.get("reason") or "preparation_failed",
                ))
    elif stage == "reasoning":
        from incident_copilot.graph.investigation_runtime import InvestigationRuntime

        from .runtime_evidence import RuntimeEvidenceClient, effective_scope
        runtime_client = RuntimeEvidenceClient(store, incident_id) if run.get("runtime_scope") else None

        async def progress(update: dict) -> None:
            # This activity owns the evidence snapshot throughout reasoning. Updating
            # it here also prevents the final stage write from losing streamed data.
            evidence.update({key: value for key, value in update.items() if key in {
                "investigation", "retrieved_evidence", "investigation_events", "graph_context", "validation_errors", "guardrails", "reasoning",
            }})
            redacted = Configuration(store, activate=False).redact(evidence)
            store.update("incidents", incident_id, {"evidence": redacted, "updated_at": utcnow()})

        async with reasoning_heartbeat(activity) as heartbeat:
            if runtime_client:
                evidence.update(await runtime_client.baseline())
                with store.connection() as db:
                    scope = effective_scope(db, run).model_dump(mode="json")
                context = evidence.get("parsed_log", {}).get("incident_context") or {}
                primary = next((group for group in context.get("groups", []) if group["id"] == context.get("primary_group_id")), None)
                if primary and scope.get("trigger_event_id") and scope["trigger_event_id"] not in primary["event_ids"]:
                    # Automatic evidence belongs to its triggering group, not another group selected by source ranking.
                    evidence["runtime_evidence"] = []
                    evidence["runtime_collection"] = [{"id": "primary-group", "capability": "runtime", "status": "unavailable",
                        "reason": "Runtime snapshot belongs to another log group; excluded from diagnosis.", "duration_ms": 0}]
                    runtime_client, scope = None, None
            else:
                scope = None
            state = await run_diagnosis_graph({
                "incident_id": incident_id, "parsed_log": evidence.get("parsed_log", {}),
                "candidates": evidence.get("candidates", []), "deep_analysis": evidence.get("deep_analysis", {}),
                "candidate_limit": config.top_k_candidates,
                "graph_context": evidence.get("graph_context"),
                "runtime_config": analysis_configuration((run.get("deployment_context") or {}).get("runtime_config")),
                "runtime_evidence": evidence.get("runtime_evidence", []),
                "runtime_collection": evidence.get("runtime_collection", []),
                "runtime_scope": scope, "source_unavailable": bool(inputs.get("source_unavailable")),
            }, config, runtime_context=InvestigationRuntime(
                repo_root=root.resolve(), index_db_path=Path(run["index_db_path"]),
                execution_id=incident_id, progress=progress, heartbeat=heartbeat,
                runtime_client=runtime_client,
            ))
        await progress({key: state[key] for key in (
            "investigation", "retrieved_evidence", "investigation_events", "graph_context", "validation_errors", "guardrails", "reasoning",
        ) if key in state})
        report = build_report(state).model_dump(mode="json")
        report["repository_source"] = run.get("repository_source")
        report["deployment_context"] = run.get("deployment_context")
        report["source_resolution"] = run.get("source_resolution", {"status": "unknown"})
        if run.get("repository_source"):
            from .repository_source import public_snapshot_payload
            report = public_snapshot_payload(report, str(root), run["repository_source"]["remote_url"])
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
