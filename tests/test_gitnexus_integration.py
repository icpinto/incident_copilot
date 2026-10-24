"""Opt-in contract test against GitNexus 1.6.12 serving a committed language fixture."""
import os
from pathlib import Path

import pytest

from incident_copilot.analysis.code_graph import retrieve_graph_context
from incident_copilot.core.settings import Settings
from incident_copilot.evaluation.golden_cases import GOLDEN_CASES
from incident_copilot.indexing.repo_indexer import index_repository
from incident_copilot.indexing.storage import IndexStorage

pytestmark = pytest.mark.skipif(os.getenv('IC_RUN_GITNEXUS_TESTS') != '1', reason='Requires the prepared GitNexus sidecar')


@pytest.mark.asyncio
async def test_prepared_graph_against_labeled_language_fixture(tmp_path):
    root = Path(os.environ['IC_GRAPH_TEST_REPO_ROOT']).resolve()
    language = os.getenv('IC_GRAPH_TEST_LANGUAGE', 'python')
    assert language in {'python', 'typescript'}
    config = Settings(code_graph_enabled=True)
    db = tmp_path / 'index.db'
    summary = index_repository(root, None, db)
    assert summary.repo_revision, 'Use a clean, committed fixture checkout'
    storage = IndexStorage(db)
    for case in (c for c in GOLDEN_CASES if c['language'] == language):
        record = storage.get_file_record(case['actual_file'])
        target = next(s for s in record.symbols if s.name == case['symbol'] and s.line_start == case['line'])
        candidate = {'file_path': record.file_path, 'symbol_name': target.name, 'line_start': target.line_start,
                     'line_end': target.line_end, 'score': 42, 'reasons': []}
        context = await retrieve_graph_context(root, db, [candidate], config)
        assert context['status'] in {'available', 'partial'}, context
        names = {s['uid']: s['name'] for s in context['symbols']}
        edges = {(names[e['source_uid']], names[e['target_uid']]) for e in context['relationships']}
        assert set(map(tuple, case['expected_edges'])) <= edges
        if case['duplicate']:
            assert ('retry', 'ping') not in edges
        if case['dynamic']:
            assert not edges