"""The ten labeled cases exercise real source indexing with a controlled provider."""
import shutil
from contextlib import asynccontextmanager

import pytest

from incident_copilot.analysis import code_graph as graph
from incident_copilot.core.settings import Settings
from incident_copilot.evaluation.golden_cases import GOLDEN_CASES
from incident_copilot.indexing import repo_indexer
from incident_copilot.indexing.storage import IndexStorage


@pytest.mark.asyncio
@pytest.mark.parametrize('case', GOLDEN_CASES, ids=lambda c: c['id'])
async def test_labeled_source_locations_and_graph_evidence(case, tmp_path, monkeypatch):
    revision = 'c' * 40
    root = tmp_path / 'source'
    shutil.copytree(case['fixture'], root)
    monkeypatch.setattr(repo_indexer, 'clean_revision', lambda _: revision)
    monkeypatch.setattr(graph, 'clean_revision', lambda _: revision)
    db = tmp_path / 'index.db'
    repo_indexer.index_repository(root, None, db)
    storage = IndexStorage(db)
    records = storage.get_file_records()
    all_symbols = [(r.file_path, s) for r in records for s in r.symbols if s.kind == 'function']
    def find(name, file=None, line=None):
        return [(p, s) for p, s in all_symbols if s.name == name and (file is None or p == file)
                and (line is None or s.line_start == line)]
    anchor_path, anchor = find(case['symbol'], case['actual_file'], case['line'])[0]
    def raw(p, s):
        return {'uid': f'{p}:{s.name}:{s.line_start}', 'name': s.name, 'kind': 'Function',
                'filePath': p, 'startLine': s.line_start, 'endLine': s.line_end}
    def ref(p, s):
        return {k: v for k, v in raw(p, s).items() if k not in {"startLine", "endLine"}}
    checkout, charge, handler = find('checkout')[0], find('charge')[0], find('handle_request')[0]
    primary, secondary = find('retry')
    ping = find('ping')[0]
    edges = [(checkout, charge), (handler, checkout), (primary, charge), (secondary, ping)]
    class Provider:
        version = graph.GITNEXUS_VERSION
        async def call(self, tool, args):
            if tool == 'list_repos':
                return {'repositories': [{'path': str(root), 'lastCommit': revision, 'indexedAt': 'fixed'}]}
            matches = [(p, s) for p, s in all_symbols if (raw(p, s)['uid'] == args['uid'] if 'uid' in args
                        else p == args['file_path'] and s.name == args['name'])]
            if len(matches) != 1:
                return {'status': 'ambiguous', 'candidates': [dict(raw(p, s), line=s.line_start) for p, s in matches]}
            pair = matches[0]
            return {'status': 'found', 'symbol': raw(*pair),
                    'incoming': {'calls': [ref(*a) for a, b in edges if b == pair]},
                    'outgoing': {'calls': [ref(*b) for a, b in edges if a == pair]},
                    'epistemic': 'lower-bound' if case['dynamic'] else 'exact'}
    @asynccontextmanager
    async def provider(_):
        yield Provider()
    monkeypatch.setattr(graph, 'open_provider', provider)
    config = Settings(_env_file=None, code_graph_enabled=True, gitnexus_repository=str(root))
    candidate = {'file_path': anchor_path, 'symbol_name': anchor.name, 'line_start': anchor.line_start,
                 'line_end': anchor.line_end, 'score': 42, 'reasons': []}
    context = await graph.retrieve_graph_context(root, db, [candidate], config)
    assert context['status'] in {'available', 'partial'}, context
    symbols = {s['uid']: s['name'] for s in context['symbols']}
    observed = {(symbols[e['source_uid']], symbols[e['target_uid']]) for e in context['relationships']}
    assert set(map(tuple, case['expected_edges'])) <= observed
    if case['dynamic']:
        assert not observed and context['completeness'] == 'lower_bound'
    if case['duplicate']:
        assert ('retry', 'ping') not in observed