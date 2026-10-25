from types import SimpleNamespace

import pytest

from incident_copilot.core.settings import Settings
from incident_copilot.evaluation import benchmark_runner
from incident_copilot.schemas.candidates import CandidateLocation


@pytest.mark.asyncio
async def test_benchmark_pairs_iterative_with_identical_graph_enriched_evidence(tmp_path, monkeypatch):
    calls = []
    graph = {
        "provider": "gitnexus", "repository": str(tmp_path), "status": "partial",
        "completeness": "lower_bound", "symbols": [], "relationships": [], "seeds": [],
    }
    candidate = CandidateLocation(file_path="cache.py", score=10, reasons=["stack"])
    monkeypatch.setattr(benchmark_runner, "index_repository", lambda *_: None)
    monkeypatch.setattr(benchmark_runner, "rank_candidates", lambda *_: (
        SimpleNamespace(items=[candidate]), SimpleNamespace(model_dump=lambda: {}),
    ))
    monkeypatch.setattr(benchmark_runner, "deep_analyze_candidate", lambda *_: {})

    async def retrieve(*_):
        return graph

    async def diagnose(payload, config, runtime_context=None):
        calls.append((payload, config.agentic_enabled, runtime_context))
        result = dict(payload, hypotheses=[{
            "rank": 1, "title": "Path bug", "explanation": "Inspect path", "evidence": [],
            "next_checks": [], "confidence": .5, "evidence_refs": [],
        }])
        if config.agentic_enabled:
            result["hypotheses"][0]["evidence_refs"] = ["source-1"]
            result["retrieved_evidence"] = [{
                "id": "source-1", "kind": "source", "repository": str(tmp_path),
                "revision": "abc", "content": "return key",
            }]
            result["investigation"] = {
                "rounds": 1, "model_calls": 2, "tool_calls": 1, "elapsed_ms": 12,
                "stop_reason": "model_finished",
            }
        return result

    monkeypatch.setattr(benchmark_runner, "retrieve_graph_context", retrieve)
    monkeypatch.setattr(benchmark_runner, "run_diagnosis_graph", diagnose)
    config = Settings(_env_file=None, agentic_enabled=True, data_dir=tmp_path, index_db_path=tmp_path / "index.db")
    result = await benchmark_runner.run_comparison(tmp_path, None, "cache failed", config)
    assert [enabled for _, enabled, _ in calls] == [False, False, True]
    assert calls[1][0] == calls[2][0]
    assert calls[2][2].repo_root == tmp_path
    metrics = result["metrics"]["iterative"]
    assert metrics["model_calls"] == 2 and metrics["tool_calls"] == 1
    assert metrics["retrieved_evidence_count"] == metrics["valid_citation_count"] == 1
    assert metrics["citation_validity"] == 1
    assert metrics["retrieved_content_utf8_bytes"] == len(b"return key")
    assert metrics["hypothesis_quality"] is None
