from pathlib import Path

from incident_copilot.analysis.dependency_probe import deep_analyze_candidate
from incident_copilot.correlation.candidate_ranker import rank_candidates
from incident_copilot.core.settings import settings
from incident_copilot.graph.diagnosis_graph import run_diagnosis_graph
from incident_copilot.indexing.repo_indexer import index_repository
from incident_copilot.reporting.report_builder import build_report
from incident_copilot.schemas.incident import IncidentInput

try:
    from temporalio import activity  # type: ignore
except Exception:  # pragma: no cover
    class DummyActivity:
        def defn(self, fn): return fn
    activity = DummyActivity()


@activity.defn
async def build_service_map_and_index_activity(repo_root: str, compose_path: str | None) -> dict:
    summary = index_repository(
        repo_root=Path(repo_root),
        compose_path=Path(compose_path) if compose_path else None,
        db_path=settings.index_db_path,
    )
    return summary.model_dump()


@activity.defn
async def correlate_logs_activity(incident: dict) -> dict:
    inc = IncidentInput.model_validate(incident)
    candidates, parsed_log = rank_candidates(
        repo_root=inc.repo_root,
        index_db_path=settings.index_db_path,
        log_text=inc.log_text,
        top_k=settings.top_k_candidates,
    )
    return {
        "parsed_log": parsed_log.model_dump(),
        "candidates": [c.model_dump() for c in candidates.items],
    }


@activity.defn
async def deep_analysis_activity(repo_root: str, candidates: list[dict]) -> dict:
    results: dict = {}
    for candidate in candidates[: settings.deep_analysis_top_n]:
        file_path = candidate["file_path"]
        abs_path = Path(repo_root) / file_path
        if abs_path.exists():
            results[file_path] = deep_analyze_candidate(abs_path)
    return results


@activity.defn
async def diagnosis_graph_activity(incident_id: str, parsed_log: dict, candidates: list[dict], deep_analysis: dict) -> dict:
    state = await run_diagnosis_graph(
        {
            "incident_id": incident_id,
            "parsed_log": parsed_log,
            "candidates": candidates,
            "deep_analysis": deep_analysis,
        }
    )
    return build_report(state).model_dump()
