from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import typer
from rich.console import Console
from rich.markdown import Markdown

if __package__ is None or __package__ == "":  # pragma: no cover
    sys.path.append(str(Path(__file__).resolve().parents[2] / "src"))

from incident_copilot.analysis.code_graph import retrieve_graph_context
from incident_copilot.analysis.dependency_probe import deep_analyze_candidate
from incident_copilot.core.logging import configure_logging
from incident_copilot.correlation.candidate_ranker import rank_candidates
from incident_copilot.graph.diagnosis_graph import run_diagnosis_graph
from incident_copilot.graph.investigation_runtime import InvestigationRuntime
from incident_copilot.indexing.repo_indexer import index_repository
from incident_copilot.reporting.markdown_renderer import render_markdown
from incident_copilot.reporting.report_builder import build_report
from incident_copilot.workflows.temporal_activities import runtime_settings

app = typer.Typer(help="Incident Copilot CLI")
console = Console()


@app.command()
def reindex(repo_root: Path, compose_path: Path | None = None) -> None:
    settings = runtime_settings()
    configure_logging()
    summary = index_repository(repo_root=repo_root, compose_path=compose_path, db_path=settings.index_db_path)
    console.print(summary.model_dump_json(indent=2))


@app.command()
def diagnose(
    repo_root: Path,
    compose_path: Path | None = None,
    log_file: Path | None = None,
    log_text: str | None = None,
    output_json: Path | None = None,
    output_md: Path | None = None,
) -> None:
    settings = runtime_settings()
    configure_logging()
    if not log_text and not log_file:
        raise typer.BadParameter("Provide either --log-text or --log-file")
    if log_file:
        log_text = log_file.read_text(encoding="utf-8", errors="ignore")
    assert log_text is not None

    execution_id = str(uuid4())
    settings = settings.model_copy(update={
        "index_db_path": settings.index_db_path.parent / "runs" / execution_id / settings.index_db_path.name,
    })
    index_repository(repo_root=repo_root, compose_path=compose_path, db_path=settings.index_db_path)
    candidates, parsed_log = rank_candidates(
        repo_root=repo_root,
        index_db_path=settings.index_db_path,
        log_text=log_text,
        top_k=settings.top_k_candidates,
    )
    deep_analysis = {
        candidate.file_path: deep_analyze_candidate(repo_root / candidate.file_path)
        for candidate in candidates.items[:settings.deep_analysis_top_n]
        if (repo_root / candidate.file_path).is_file()
    }
    graph_context = asyncio.run(retrieve_graph_context(
        repo_root, settings.index_db_path, [c.model_dump() for c in candidates.items], settings,
    ))
    state = asyncio.run(
        run_diagnosis_graph(
            {
                "incident_id": str(uuid4()),
                "parsed_log": parsed_log.model_dump(),
                "candidates": [c.model_dump() for c in candidates.items],
                "deep_analysis": deep_analysis,
                "graph_context": graph_context,
                "candidate_limit": settings.top_k_candidates,
            }, settings,
            runtime_context=InvestigationRuntime(
                repo_root=repo_root.resolve(), index_db_path=settings.index_db_path,
                execution_id=execution_id,
            ),
        )
    )
    report = build_report(state)

    if output_json:
        output_json.write_text(json.dumps(report.model_dump(), indent=2), encoding="utf-8")
    markdown = render_markdown(report, output_md)
    console.print(Markdown(markdown))


if __name__ == "__main__":
    app()
