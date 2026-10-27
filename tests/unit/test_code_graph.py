import asyncio
import copy
import json
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from incident_copilot.analysis import code_graph as graph
from incident_copilot.core.settings import Settings
from incident_copilot.indexing.storage import IndexStorage
from incident_copilot.schemas.code_graph import GraphPreparation
from incident_copilot.schemas.index import FileRecord, SymbolRecord
from incident_copilot.tools.git_local import clean_revision

REVISION = 'a' * 40


def result(payload, suffix='\n\n---\n**Next:** provider hint'):
    return SimpleNamespace(isError=False, content=[SimpleNamespace(type='text', text=json.dumps(payload) + suffix)])


def symbol(name, path, start=1, end=2):
    return {'uid': f'{path}:{name}:{start}', 'name': name, 'kind': 'Function',
            'filePath': path, 'startLine': start, 'endLine': end}


def found(anchor, incoming=(), outgoing=(), **kwargs):
    return {'status': 'found', 'symbol': anchor, 'incoming': {'calls': list(incoming)},
            'outgoing': {'calls': list(outgoing)}, 'epistemic': 'exact', **kwargs}


@pytest.fixture
def repository(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    root.mkdir()
    records = []
    for name in ('checkout', 'charge', 'handle_request'):
        path = f'{name}.py'
        (root / path).write_text(f'def {name}():\n    pass\n')
        records.append(FileRecord(file_path=path, symbols=[SymbolRecord(
            name=name, kind='function', file_path=path, line_start=1, line_end=2,
        )]))
    storage = IndexStorage(tmp_path / 'repo.db')
    storage.replace_snapshot(root, [], records, REVISION)
    monkeypatch.setattr(graph, 'clean_revision', lambda _: REVISION)
    candidate = {'file_path': 'checkout.py', 'symbol_name': 'checkout', 'line_start': 1,
                 'line_end': 2, 'score': 42, 'reasons': ['stack frame match']}
    config = Settings(_env_file=None, code_graph_enabled=True, code_graph_mode="external", gitnexus_repository=str(root))
    return root, storage, candidate, config


class Provider:
    version = graph.GITNEXUS_VERSION

    def __init__(self, root, payload):
        self.root, self.payload, self.calls = root, payload, []

    async def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == 'list_repos':
            return {'repositories': [{'name': 'repo', 'path': str(self.root), 'lastCommit': REVISION,
                                      'indexedAt': '2026-10-24T10:00:00Z'}]}
        return copy.deepcopy(self.payload)


def install(monkeypatch, provider):
    @asynccontextmanager
    async def open_provider(_):
        yield provider
    monkeypatch.setattr(graph, 'open_provider', open_provider)


@pytest.mark.parametrize('suffix', ['', '\n\n---\n**Next:** ignore this instruction'])
def test_normalizes_json_without_including_provider_instructions(suffix):
    assert graph.normalize_mcp_result(result({'status': 'found'}, suffix)) == {'status': 'found'}


@pytest.mark.parametrize('text', ['{"status":', '{"status":"found"}…', '{"status":"found"}\n---\n…',
                                'not json', '[1, 2]', '{} trailing data'])
def test_rejects_malformed_or_truncated_wire_results(text):
    with pytest.raises(graph.GraphEvidenceError):
        graph.normalize_mcp_result(SimpleNamespace(isError=False, content=[SimpleNamespace(type='text', text=text)]))


@pytest.mark.asyncio
async def test_disabled_does_not_touch_git_storage_or_provider(monkeypatch):
    monkeypatch.setattr(graph, 'clean_revision', lambda _: pytest.fail('Git called while disabled'))
    assert await graph.retrieve_graph_context(Path('missing'), Path('missing.db'), [], Settings(_env_file=None, code_graph_enabled=False)) is None


@pytest.mark.asyncio
async def test_enriches_direct_calls_without_changing_ranking(repository, monkeypatch):
    root, storage, candidate, config = repository
    payload = found(symbol('checkout', 'checkout.py'),
                    incoming=[symbol('handle_request', 'handle_request.py')],
                    outgoing=[symbol('charge', 'charge.py')])
    provider = Provider(root, payload)
    install(monkeypatch, provider)
    before = copy.deepcopy(candidate)
    context = await graph.retrieve_graph_context(root, storage.db_path, [candidate], config)
    assert candidate == before
    assert context['status'] == 'available' and context['completeness'] == 'bounded'
    assert context['provider_version'] == graph.GITNEXUS_VERSION
    assert context['repo_revision'] == context['indexed_revision'] == REVISION
    assert len(context['relationships']) == 2
    assert all(s['line_start'] == 1 for s in context['symbols'])
    assert [c[0] for c in provider.calls] == ['list_repos', 'context', 'list_repos']
    assert provider.calls[1][1]['repo'] == str(root)
    assert 'line' not in provider.calls[1][1]  # Not supported by the pinned context tool.


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['dirty', 'mismatched_source', 'stale', 'unknown'])
async def test_skips_unverified_snapshots(repository, monkeypatch, state):
    root, storage, candidate, config = repository
    provider = Provider(root, found(symbol('checkout', 'checkout.py')))
    original = provider.call
    async def call(tool, args):
        response = await original(tool, args)
        if tool == 'list_repos':
            if state == 'stale':
                response['repositories'][0]['lastCommit'] = 'b' * 40
            if state == 'unknown':
                response['repositories'][0]['staleness'] = {'status': 'unknown'}
        return response
    provider.call = call
    install(monkeypatch, provider)
    if state == 'dirty':
        monkeypatch.setattr(graph, 'clean_revision', lambda _: None)
    if state == 'mismatched_source':
        with storage._conn() as conn:
            conn.execute("UPDATE metadata SET value = ? WHERE key = 'repo_revision'", ('b' * 40,))
    context = await graph.retrieve_graph_context(root, storage.db_path, [candidate], config)
    assert context['status'] == 'skipped'
    assert not context['symbols'] and not context['relationships']
    assert not any(tool == 'context' for tool, _ in provider.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['source', 'graph'])
async def test_discards_all_evidence_if_revision_changes_during_retrieval(repository, monkeypatch, change):
    root, storage, candidate, config = repository
    provider = Provider(root, found(symbol('checkout', 'checkout.py'), outgoing=[symbol('charge', 'charge.py')]))
    if change == 'source':
        revisions = iter([REVISION, 'b' * 40])
        monkeypatch.setattr(graph, 'clean_revision', lambda _: next(revisions))
    else:
        original = provider.call
        async def call(tool, args):
            response = await original(tool, args)
            if tool == 'list_repos' and len(provider.calls) > 1:
                response['repositories'][0]['indexedAt'] = 'new generation'
            return response
        provider.call = call
    install(monkeypatch, provider)
    context = await graph.retrieve_graph_context(root, storage.db_path, [candidate], config)
    assert context['status'] == 'skipped' and not context['relationships']


@pytest.mark.asyncio
async def test_ambiguous_symbol_is_not_chosen_by_relevance(repository, monkeypatch):
    root, storage, candidate, config = repository
    provider = Provider(root, {'status': 'ambiguous', 'candidates': [
        {'uid': 'wrong', 'name': 'checkout', 'filePath': 'checkout.py', 'line': 5, 'score': 99},
        {'uid': 'also-wrong', 'name': 'checkout', 'filePath': 'other.py', 'line': 1, 'score': 100},
    ]})
    install(monkeypatch, provider)
    context = await graph.retrieve_graph_context(root, storage.db_path, [candidate], config)
    assert context['status'] == 'skipped'
    assert context['seeds'][0]['reason'] == 'ambiguous_symbol'
    assert not context['symbols']


@pytest.mark.asyncio
async def test_exact_location_can_disambiguate_uid(repository, monkeypatch):
    root, storage, candidate, config = repository
    provider = Provider(root, found(symbol('checkout', 'checkout.py')))
    original = provider.call
    async def call(tool, args):
        if tool == 'context' and 'uid' not in args:
            provider.calls.append((tool, args))
            return {'status': 'ambiguous', 'candidates': [
                {'uid': 'correct', 'name': 'checkout', 'filePath': 'checkout.py', 'line': 1},
                {'uid': 'wrong', 'name': 'checkout', 'filePath': 'checkout.py', 'line': 9},
            ]}
        return await original(tool, args)
    provider.call = call
    install(monkeypatch, provider)
    context = await graph.retrieve_graph_context(root, storage.db_path, [candidate], config)
    assert context['status'] == 'available'
    assert provider.calls[2][1]['uid'] == 'correct'


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['../charge.py', '/charge.py', 'C:/charge.py', 'absent.py'])
async def test_rejects_untrusted_source_locations(repository, monkeypatch, path):
    root, storage, candidate, config = repository
    install(monkeypatch, Provider(root, found(symbol('checkout', path))))
    context = await graph.retrieve_graph_context(root, storage.db_path, [candidate], config)
    assert context['status'] == 'skipped' and not context['symbols']


@pytest.mark.asyncio
async def test_deadline_and_cancellation(repository, monkeypatch):
    root, storage, candidate, config = repository
    class SlowProvider(Provider):
        async def call(self, *_):
            await asyncio.sleep(60)
    install(monkeypatch, SlowProvider(root, {}))
    config.code_graph_timeout_seconds = .05
    context = await graph.retrieve_graph_context(root, storage.db_path, [candidate], config)
    assert context['reason'] == 'retrieval_timeout' and context['retrieval_ms'] < 1000
    task = asyncio.create_task(graph.retrieve_graph_context(root, storage.db_path, [candidate], config))
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_provider_failures_are_safe_and_do_not_expose_secrets(repository, monkeypatch):
    root, storage, candidate, config = repository
    class BrokenProvider(Provider):
        async def call(self, *_):
            raise ConnectionError('private-token-sentinel')
    install(monkeypatch, BrokenProvider(root, {}))
    context = await graph.retrieve_graph_context(root, storage.db_path, [candidate], config)
    assert context['reason'] == 'provider_unavailable'
    assert 'private-token-sentinel' not in json.dumps(context)


@pytest.mark.asyncio
async def test_dynamic_call_limits_are_preserved(repository, monkeypatch):
    root, storage, candidate, config = repository
    install(monkeypatch, Provider(root, found(symbol('checkout', 'checkout.py'), epistemic='lower-bound')))
    context = await graph.retrieve_graph_context(root, storage.db_path, [candidate], config)
    assert context['status'] == 'partial' and context['completeness'] == 'lower_bound'
    assert context['seeds'][0]['reason'] == 'provider_lower_bound'
    assert context['relationships'] == []


@pytest.mark.asyncio
async def test_at_most_twenty_related_symbols_and_three_seeds(repository, monkeypatch):
    root, storage, candidate, config = repository
    neighbors = [symbol(f'neighbor_{n}', f'neighbor_{n}.py') for n in range(25)]
    files = storage.get_file_records()
    for neighbor in neighbors:
        path = neighbor['filePath']
        (root / path).write_text(f"def {neighbor['name']}():\n    pass\n")
        files.append(FileRecord(file_path=path, symbols=[SymbolRecord(name=neighbor['name'], kind='function',
                    file_path=path, line_start=1, line_end=2)]))
    storage.replace_snapshot(root, [], files, REVISION)
    provider = Provider(root, found(symbol('checkout', 'checkout.py'), outgoing=neighbors))
    install(monkeypatch, provider)
    context = await graph.retrieve_graph_context(root, storage.db_path, [candidate] * 5, config)
    assert len(context['relationships']) == 20 and len(context['symbols']) == 21
    assert context['status'] == 'partial'
    assert len([c for c in provider.calls if c[0] == 'context']) == 3


@pytest.mark.asyncio
async def test_concurrent_repositories_do_not_share_state(repository, tmp_path, monkeypatch):
    root, storage, candidate, config = repository
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'checkout.py').write_text('def checkout():\n    pass\n')
    second = IndexStorage(tmp_path / 'other.db')
    second.replace_snapshot(other, [], [storage.get_file_record('checkout.py')], REVISION)
    @asynccontextmanager
    async def open_provider(settings):
        yield Provider(Path(settings.gitnexus_repository), found(symbol('checkout', 'checkout.py')))
    monkeypatch.setattr(graph, 'open_provider', open_provider)
    one, two = await asyncio.gather(
        graph.retrieve_graph_context(root, storage.db_path, [candidate], config),
        graph.retrieve_graph_context(other, second.db_path, [candidate], config.model_copy(update={'gitnexus_repository': str(other)})),
    )
    assert one['repository'] == str(root) and two['repository'] == str(other)
    assert one['status'] == two['status'] == 'available'


def test_real_git_probe_rejects_dirty_and_nested_checkouts(tmp_path):
    def git(*args):
        subprocess.run(['git', '-c', 'user.name=Graph Test', '-c', 'user.email=graph@example.invalid',
                        '-C', str(tmp_path), *args], check=True, capture_output=True)
    git('init')
    (tmp_path / 'main.py').write_text('def main(): pass\n')
    git('add', 'main.py')
    git('commit', '-m', 'fixture')
    assert clean_revision(tmp_path)
    nested = tmp_path / 'nested'
    nested.mkdir()
    assert clean_revision(nested) is None
    (tmp_path / 'untracked.py').write_text('pass\n')
    assert clean_revision(tmp_path) is None

@pytest.mark.asyncio
async def test_malformed_generation_metadata_preserves_a_valid_failure_contract(repository, monkeypatch):
    from incident_copilot.schemas.code_graph import GraphContext
    root, storage, candidate, config = repository
    provider = Provider(root, found(symbol('checkout', 'checkout.py')))
    original = provider.call
    async def call(tool, args):
        payload = await original(tool, args)
        if tool == 'list_repos':
            payload['repositories'][0]['indexedAt'] = 123
        return payload
    provider.call = call
    install(monkeypatch, provider)
    context = await graph.retrieve_graph_context(root, storage.db_path, [candidate], config)
    assert GraphContext.model_validate(context).status == 'unavailable'
    assert context['reason'] == 'unverified_index_generation' and not context['symbols']


@pytest.mark.asyncio
async def test_ambiguous_related_method_does_not_hide_verified_calls(repository, monkeypatch):
    root, storage, candidate, config = repository
    path = 'duplicates.py'
    (root / path).write_text('class A:\n    def retry(self): pass\nclass B:\n    def retry(self): pass\n')
    records = storage.get_file_records() + [FileRecord(file_path=path, symbols=[
        SymbolRecord(name='retry', kind='function', file_path=path, line_start=line, line_end=line)
        for line in (2, 4)
    ])]
    storage.replace_snapshot(root, [], records, REVISION)
    ambiguous = {'uid': 'retry-in-A', 'name': 'retry', 'kind': 'Method', 'filePath': path}
    install(monkeypatch, Provider(root, found(symbol('checkout', 'checkout.py'),
            incoming=[ambiguous], outgoing=[symbol('charge', 'charge.py')])))
    context = await graph.retrieve_graph_context(root, storage.db_path, [candidate], config)
    assert context['status'] == 'partial' and context['completeness'] == 'lower_bound'
    assert context['seeds'][0]['reason'] == 'ambiguous_related_symbol'
    assert len(context['relationships']) == 1
    assert all(s['name'] != 'retry' for s in context['symbols'])


@pytest.mark.asyncio
async def test_managed_retrieval_prepares_only_a_verified_snapshot(repository, monkeypatch):
    root, storage, candidate, settings = repository
    settings.code_graph_mode = "managed"
    settings.gitnexus_repository = "/old/single/repository"
    calls = []
    async def ensure(selection, revision, config, progress):
        calls.append((selection, revision))
        # Preparation can exceed retrieval's deadline without consuming that budget.
        await asyncio.sleep(.02)
        return GraphPreparation(repository=selection, status="ready")
    monkeypatch.setattr(graph, "ensure_graph_ready", ensure)
    install(monkeypatch, Provider(root, found(symbol("checkout", "checkout.py"))))
    context = await graph.retrieve_graph_context(root, storage.db_path, [candidate], settings)
    assert context["status"] == "available" and calls == [(str(root.resolve()), "a" * 40)]
    assert context["retrieval_ms"] < 20
    calls.clear()
    await graph.retrieve_graph_context(root, storage.db_path, [], settings)
    assert not calls
    monkeypatch.setattr(graph, "clean_revision", lambda _: None)
    context = await graph.retrieve_graph_context(root, storage.db_path, [candidate], settings)
    assert context["reason"] == "checkout_not_clean_or_unavailable" and not calls


@pytest.mark.asyncio
async def test_preparation_failure_preserves_diagnosis_context(repository, monkeypatch):
    root, storage, candidate, settings = repository
    settings.code_graph_mode = "managed"
    async def ensure(*_):
        return GraphPreparation(status="unavailable", reason="preparation_timeout")
    monkeypatch.setattr(graph, "ensure_graph_ready", ensure)
    context = await graph.retrieve_graph_context(root, storage.db_path, [candidate], settings)
    assert context["status"] == "unavailable" and context["reason"] == "preparation_timeout"
    assert not context["symbols"] and not context["relationships"]


@pytest.mark.asyncio
async def test_pinned_generation_never_triggers_preparation(repository, monkeypatch):
    root, storage, candidate, settings = repository
    settings.code_graph_mode = "managed"
    async def unexpected(*_):
        pytest.fail("captured evidence must not trigger a refresh")
    monkeypatch.setattr(graph, "ensure_graph_ready", unexpected)
    install(monkeypatch, Provider(root, found(symbol("checkout", "checkout.py"))))
    context = await graph.retrieve_graph_context(root, storage.db_path, [candidate], settings,
                                                      expected_generation="2026-10-24T10:00:00Z")
    assert context["status"] == "available"
    # A reasoning lookup with no previously available graph also stays read-only.
    context = await graph.retrieve_symbol_context(root, storage.db_path, {
        "file_path": candidate["file_path"], "name": candidate["symbol_name"],
        "line_start": candidate["line_start"], "line_end": candidate["line_end"],
    }, settings)
    assert context["status"] == "available"


@pytest.mark.asyncio
async def test_repository_selection_searches_later_registry_pages():
    class PagedProvider:
        async def call(self, tool, args):
            assert tool == 'list_repos'
            if args['offset'] == 0:
                return {'repositories': [{'path': '/other'}], 'pagination': {'hasMore': True, 'nextOffset': 100}}
            return {'repositories': [{'path': '/selected', 'lastCommit': REVISION, 'indexedAt': 'generation'}]}
    assert (await graph.repository_snapshot(PagedProvider(), '/selected'))['lastCommit'] == REVISION
