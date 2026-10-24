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
    """Index/rank once, use identical evidence/model settings, and vary graph context only."""
    await asyncio.to_thread(index_repository, root, compose, config.index_db_path)
    candidates, parsed = await asyncio.to_thread(rank_candidates, root, config.index_db_path, logs, config.top_k_candidates)
    files = {}
    for candidate in candidates.items[:config.deep_analysis_top_n]:
        files[candidate.file_path] = await asyncio.to_thread(deep_analyze_candidate, root / candidate.file_path)
    state: dict[str, Any] = {'incident_id': 'code-graph-comparison', 'parsed_log': parsed.model_dump(),
             'candidates': [c.model_dump() for c in candidates.items], 'deep_analysis': files,
             'candidate_limit': config.top_k_candidates}
    baseline = await run_diagnosis_graph(copy.deepcopy(state), config)
    tracemalloc.start()
    started = time.monotonic()
    try:
        context = await retrieve_graph_context(root, config.index_db_path, state['candidates'],
                                              config.model_copy(update={'code_graph_enabled': True}))
        retrieval_seconds = time.monotonic() - started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    enriched = await run_diagnosis_graph(copy.deepcopy(state) | {'graph_context': context}, config)
    baseline_report, enriched_report = build_report(dict(baseline)), build_report(dict(enriched))
    if baseline_report.candidates != enriched_report.candidates:
        raise AssertionError('Graph enrichment changed candidate ranking')
    symbols = {s['uid']: s for s in (context or {}).get('symbols', [])}
    observed = {(symbols[e['source_uid']]['name'], symbols[e['target_uid']]['name'])
                for e in (context or {}).get('relationships', [])}
    expected = {tuple(edge) for edge in expected_edges} if expected_edges is not None else None
    def prompt_metrics(value):
        size = len(build_diagnosis_prompt(value).encode('utf-8'))
        return {'utf8_bytes': size, 'estimated_tokens_at_four_bytes': (size + 3) // 4}
    result = {
        'model': {'provider': config.llm_provider, 'model': config.ollama_model if config.llm_provider == 'ollama' else config.openai_compatible_model},
        'baseline': baseline_report.model_dump(mode='json'), 'enriched': enriched_report.model_dump(mode='json'),
        'metrics': {'candidate_ranking_unchanged': True,
                    'top_5_hit': top_k_hit(actual_file, [c.file_path for c in candidates.items]) if actual_file else None,
                    'expected_edges': len(expected) if expected is not None else None,
                    'matched_expected_edges': len(expected & observed) if expected is not None else None,
                    'additional_edges_for_review': sorted(observed - expected) if expected is not None else None,
                    'retrieval_seconds': retrieval_seconds, 'client_python_peak_allocated_bytes': peak,
                    'sidecar_memory_bytes': None,
                    'prompt_baseline': prompt_metrics(baseline), 'prompt_enriched': prompt_metrics(enriched),
                    'unsupported_claims': None, 'hypothesis_quality': None},
        'review': 'Compare hypotheses against labeled source and incident facts; record unsupported claims and quality manually. Python allocation peak excludes native and sidecar memory. Token counts are estimates. Check engine/fallback_reason before comparing LLM quality.',
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