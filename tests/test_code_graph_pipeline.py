import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from incident_copilot.dashboard import activities
from incident_copilot.dashboard.models import IncidentSubmission, SettingsUpdate
from incident_copilot.dashboard.service import DashboardService
from incident_copilot.dashboard.store import ControlStore, utcnow
from incident_copilot.graph.nodes import build_hypotheses
from incident_copilot.reporting.markdown_renderer import render_markdown
from incident_copilot.schemas.diagnosis import DiagnosisReport
from incident_copilot.schemas.incident import IncidentInput
from incident_copilot.workflows import temporal_activities


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    monkeypatch.setenv('IC_CONTROL_DB_PATH', str(tmp_path / 'control.db'))
    monkeypatch.setenv('IC_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('IC_INDEX_DB_PATH', str(tmp_path / 'index.db'))
    monkeypatch.setenv('IC_CODE_GRAPH_ENABLED', 'true')
    monkeypatch.setenv('IC_TREE_SITTER_ENABLED', 'false')
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'main.py').write_text('def charge():\n    raise TimeoutError("gateway")\n')
    store = ControlStore(tmp_path / 'control.db')
    service = DashboardService(store)
    service.store.put('repositories', 'repo', {'id': 'repo', 'name': 'test', 'repo_root': str(root),
        'compose_path': None, 'archived': False, 'created_at': utcnow()})
    calls, prompts = [], []
    context = {'provider': 'gitnexus', 'provider_version': '1.6.12', 'repository': str(root),
               'repo_revision': 'a' * 40, 'indexed_revision': 'a' * 40, 'status': 'available',
               'completeness': 'bounded', 'symbols': [{'uid': 'charge', 'name': 'charge', 'file_path': 'main.py', 'line_start': 1, 'line_end': 2}],
               'relationships': [], 'seeds': []}
    async def retrieve(root_arg, db, candidates, config):
        assert Path(root_arg) == root and config.code_graph_enabled
        assert candidates[0]['file_path'] == 'main.py'
        calls.append((str(root_arg), str(db)))
        return context
    class OfflineModel:
        async def complete(self, **kwargs):
            prompts.append(kwargs['prompt'])
            raise ConnectionError('model unavailable')
    monkeypatch.setattr(activities, 'retrieve_graph_context', retrieve)
    monkeypatch.setattr(temporal_activities, 'retrieve_graph_context', retrieve)
    monkeypatch.setattr(build_hypotheses, 'get_client', lambda *_: OfflineModel())
    logs = 'File "/app/main.py", line 2, in charge\nTimeoutError: gateway'
    return service, root, logs, context, calls, prompts, retrieve


@pytest.mark.asyncio
async def test_dashboard_persists_graph_for_retries_prompts_and_exports(pipeline):
    service, _root, logs, context, calls, prompts, _ = pipeline
    run = service.submit(IncidentSubmission(repository_id='repo', log_text=logs))
    for stage in ('indexing', 'correlating', 'analyzing', 'analyzing', 'reasoning'):
        await activities.dashboard_stage_activity(run.id, stage)
    saved = service.store.get('incidents', run.id)
    assert len(calls) == 1
    assert saved['evidence']['graph_context'] == context
    report = DiagnosisReport.model_validate(saved['report'])
    assert report.graph_context.provider_version == '1.6.12'
    assert report.engine == 'deterministic' and report.fallback_reason == 'llm_request_failed'
    assert '"provider_version": "1.6.12"' in prompts[0]
    assert 'Static Code Graph' in render_markdown(report)
    assert json.loads((Path(saved['artifact_dir']) / 'report.json').read_text())['graph_context']['repo_revision'] == 'a' * 40
    assert service.public(saved).evidence.graph_context.provider == 'gitnexus'


@pytest.mark.asyncio
async def test_synchronous_api_and_legacy_activity_contract(pipeline):
    service, root, logs, _context, calls, _prompts, _ = pipeline
    from apps.api.main import diagnose
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(dashboard=service)))
    report = await diagnose(IncidentInput(repo_root=root, log_text=logs), request)
    assert report.graph_context.repository == str(root)
    assert len(calls) == 1
    # Historical Temporal histories contain a files-only analysis result.
    old = await temporal_activities.diagnosis_graph_activity('historical', {}, [], {'main.py': {}}, service.configuration.effective().model_dump(mode='json'))
    assert old['graph_context'] is None


def test_cli_uses_the_same_graph_helper_and_writes_graph_json(pipeline, tmp_path, monkeypatch):
    service, root, logs, _context, calls, prompts, retrieve = pipeline
    from apps.cli import main
    monkeypatch.setattr(main, 'runtime_settings', lambda: service.configuration.effective())
    monkeypatch.setattr(main, 'retrieve_graph_context', retrieve)
    monkeypatch.setattr(main, 'console', Console(file=io.StringIO()))
    output = tmp_path / 'cli-report.json'
    main.diagnose(root, log_text=logs, output_json=output)
    assert json.loads(output.read_text())['graph_context']['provider_version'] == '1.6.12'
    assert len(calls) == 1 and '1.6.12' in prompts[0]


def test_graph_tokens_are_masked_and_historical_revisions_are_redacted(pipeline):
    service, *_ = pipeline
    config = service.configuration
    config.save(SettingsUpdate(values={'gitnexus_auth_token': 'old-graph-token'}))
    config.revision()
    view = config.save(SettingsUpdate(values={'gitnexus_auth_token': 'new-graph-token'}))
    assert view.graph_secret_configured
    assert 'graph-token' not in view.model_dump_json()
    assert config.redact({'evidence': 'old-graph-token new-graph-token'}) == {'evidence': '[redacted] [redacted]'}
    config.save(SettingsUpdate(values={'gitnexus_auth_token': ''}))
    assert config.effective().gitnexus_auth_token == 'new-graph-token'
    with pytest.raises(ValueError):
        config.save(SettingsUpdate(values={'gitnexus_url': 'http://private:token@localhost/mcp'}))
    with pytest.raises(ValueError):
        config.save(SettingsUpdate(values={'code_graph_timeout_seconds': 16}))


@pytest.mark.asyncio
async def test_comparison_keeps_ranking_and_labels_unmeasured_quality(pipeline, monkeypatch):
    service, root, logs, _context, calls, prompts, retrieve = pipeline
    from incident_copilot.evaluation import benchmark_runner
    monkeypatch.setattr(benchmark_runner, 'retrieve_graph_context', retrieve)
    result = await benchmark_runner.run_comparison(root, None, logs, service.configuration.effective(), 'main.py', [])
    assert result['metrics']['candidate_ranking_unchanged'] and result['metrics']['top_5_hit']
    assert result['metrics']['unsupported_claims'] is None and result['metrics']['sidecar_memory_bytes'] is None
    assert result['baseline']['graph_context'] is None
    assert result['enriched']['graph_context']['provider'] == 'gitnexus'
    assert len(prompts) == 2 and len(calls) == 1