"""Paired research runs. Quality and unsupported-claim judgments require human review."""
import argparse
import asyncio
import copy
import json
import time
import tracemalloc
from pathlib import Path
from typing import Any
from uuid import uuid4

from incident_copilot.analysis.code_graph import retrieve_graph_context
from incident_copilot.analysis.dependency_probe import deep_analyze_candidate
from incident_copilot.core.settings import Settings
from incident_copilot.correlation.candidate_ranker import rank_candidates
from incident_copilot.evaluation.golden_cases import GOLDEN_CASES
from incident_copilot.evaluation.metrics import top_k_hit
from incident_copilot.graph.diagnosis_graph import run_diagnosis_graph
from incident_copilot.graph.nodes.build_hypotheses import build_diagnosis_prompt
from incident_copilot.indexing.repo_indexer import index_repository
from incident_copilot.reporting.report_builder import build_report


async def run_comparison(root: Path, compose: Path | None, logs: str, config: Settings,
                         actual_file: str | None = None, expected_edges: list | None = None) -> dict:
    """Compare graph enrichment and iterative reasoning from one indexed source snapshot."""
    await asyncio.to_thread(index_repository, root, compose, config.index_db_path)
    candidates, parsed = await asyncio.to_thread(rank_candidates, root, config.index_db_path, logs, config.top_k_candidates)
    files = {}
    for candidate in candidates.items[:config.deep_analysis_top_n]:
        files[candidate.file_path] = await asyncio.to_thread(deep_analyze_candidate, root / candidate.file_path)
    state: dict[str, Any] = {'incident_id': 'code-graph-comparison', 'parsed_log': parsed.model_dump(),
             'candidates': [c.model_dump() for c in candidates.items], 'deep_analysis': files,
             'candidate_limit': config.top_k_candidates}
    single_pass = config.model_copy(update={'agentic_enabled': False})
    started = time.monotonic()
    baseline = await run_diagnosis_graph(copy.deepcopy(state), single_pass)
    baseline_seconds = time.monotonic() - started
    tracemalloc.start()
    started = time.monotonic()
    try:
        context = await retrieve_graph_context(root, config.index_db_path, state['candidates'],
                                              config.model_copy(update={'code_graph_enabled': True}))
        retrieval_seconds = time.monotonic() - started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    initial_evidence = copy.deepcopy(state) | {'graph_context': context}
    started = time.monotonic()
    enriched = await run_diagnosis_graph(copy.deepcopy(initial_evidence), single_pass)
    enriched_seconds = time.monotonic() - started
    from incident_copilot.graph.investigation_runtime import InvestigationRuntime
    started = time.monotonic()
    iterative = await run_diagnosis_graph(
        copy.deepcopy(initial_evidence), config.model_copy(update={'agentic_enabled': True}),
        runtime_context=InvestigationRuntime(root, config.index_db_path, str(uuid4())),
    )
    iterative_seconds = time.monotonic() - started
    baseline_report, enriched_report = build_report(dict(baseline)), build_report(dict(enriched))
    iterative_report = build_report(dict(iterative))
    def ranking(report):
        return [(c.file_path, c.symbol_name, c.line_start, c.line_end, c.score) for c in report.candidates]
    if ranking(baseline_report) != ranking(enriched_report) or ranking(enriched_report) != ranking(iterative_report):
        raise AssertionError('Graph enrichment changed candidate ranking')
    symbols = {s['uid']: s for s in (context or {}).get('symbols', [])}
    observed = {(symbols[e['source_uid']]['name'], symbols[e['target_uid']]['name'])
                for e in (context or {}).get('relationships', [])}
    expected = {tuple(edge) for edge in expected_edges} if expected_edges is not None else None
    def prompt_metrics(value):
        size = len(build_diagnosis_prompt(value).encode('utf-8'))
        return {'utf8_bytes': size, 'estimated_tokens_at_four_bytes': (size + 3) // 4}
    def investigation_metrics(report, seconds):
        known = {item.id for item in report.retrieved_evidence}
        citations = [ref for hypothesis in report.hypotheses for ref in hypothesis.evidence_refs]
        valid = sum(ref in known for ref in citations)
        summary = report.investigation
        return {
            'latency_seconds': seconds,
            'model_calls': summary.model_calls if summary else int(bool(report.candidates)),
            'tool_calls': summary.tool_calls if summary else 0,
            'retrieved_evidence_count': len(known),
            'retrieved_content_utf8_bytes': sum(len(item.content.encode('utf-8')) for item in report.retrieved_evidence),
            'citation_count': len(citations), 'valid_citation_count': valid,
            'citation_validity': valid / len(citations) if citations else None,
            'stop_reason': summary.stop_reason if summary else None,
            'incomplete': summary.incomplete if summary else None,
            'hypothesis_quality': None, 'unsupported_claims': None,
        }
    result = {
        'model': {'provider': config.llm_provider, 'model': config.ollama_model if config.llm_provider == 'ollama' else config.openai_compatible_model},
        'baseline': baseline_report.model_dump(mode='json'), 'enriched': enriched_report.model_dump(mode='json'),
        'iterative': iterative_report.model_dump(mode='json'),
        'metrics': {'candidate_ranking_unchanged': True,
                    'top_5_hit': top_k_hit(actual_file, [c.file_path for c in candidates.items]) if actual_file else None,
                    'expected_edges': len(expected) if expected is not None else None,
                    'matched_expected_edges': len(expected & observed) if expected is not None else None,
                    'additional_edges_for_review': sorted(observed - expected) if expected is not None else None,
                    'retrieval_seconds': retrieval_seconds, 'client_python_peak_allocated_bytes': peak,
                    'sidecar_memory_bytes': None,
                    'prompt_baseline': prompt_metrics(baseline), 'prompt_enriched': prompt_metrics(enriched),
                    'single_pass': investigation_metrics(enriched_report, enriched_seconds),
                    'iterative': investigation_metrics(iterative_report, iterative_seconds),
                    'baseline_latency_seconds': baseline_seconds,
                    'unsupported_claims': None, 'hypothesis_quality': None},
        'review': 'The enriched single-pass and iterative arms start from identical evidence. Compare hypotheses against labeled source and incident facts; record unsupported claims and quality manually. More retrieval does not establish better diagnosis. Python allocation peak excludes native and sidecar memory. Token counts are estimates. Check engine/fallback_reason before comparing LLM quality.',
    }
    # Benchmark artifacts follow the same secret-handling rule as diagnosis exports.
    def redact(value):
        if isinstance(value, dict):
            return {k: redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [redact(v) for v in value]
        if isinstance(value, str):
            for secret in (config.gitnexus_auth_token, config.openai_compatible_api_key):
                if secret and secret != 'dummy':
                    value = value.replace(secret, '[redacted]')
        return value
    return redact(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', required=True, type=Path)
    parser.add_argument('--compose-path', type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--log-file', type=Path)
    source.add_argument('--golden-case', choices=[c['id'] for c in GOLDEN_CASES])
    parser.add_argument('--actual-file')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    config = Settings()
    config.index_db_path = config.data_dir / 'benchmarks' / str(uuid4()) / 'index.db'
    case = next((c for c in GOLDEN_CASES if c['id'] == args.golden_case), None)
    logs = (f'File "/app/{case["actual_file"]}", line {case["line"]}, in {case["symbol"]}\nTimeoutError: gateway timeout'
            if case else args.log_file.read_text(encoding='utf-8'))
    result = asyncio.run(run_comparison(args.repo_root.resolve(), args.compose_path, logs, config,
                                       case['actual_file'] if case else args.actual_file,
                                       case['expected_edges'] if case else None))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(f'Saved paired comparison to {args.output}')


if __name__ == '__main__':
    main()
