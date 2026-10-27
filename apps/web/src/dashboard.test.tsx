import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from './api';
import { Incidents, NewIncident, IncidentDetail } from './pages/Incidents';
import { Settings } from './pages/Settings';
import { Repositories } from './pages/Repositories';
import { Pipeline } from './components';
import type { Incident, SettingsData } from './types';

const repo = { id: 'repo', name: 'Commerce', repo_root: '/repos/commerce', compose_path: null, archived: false, validation: null, index_summary: null, created_at: '2026-10-22T09:00:00Z' };
const run: Incident = { id: 'run-123', title: 'Payment timeout', repository_id: 'repo', repository_name: 'Commerce', status: 'completed', created_at: '2026-10-22T09:00:00Z', updated_at: '2026-10-22T09:01:00Z', completed_at: '2026-10-22T09:01:00Z', config_revision: 'rev1', stage: 'completed', timeline: [], report: { incident_id: 'run-123', likely_service: 'payments', likely_error_class: 'TimeoutError', candidates: [{ file_path: 'api/main.py', symbol_name: 'charge', service_name: 'payments', score: 24, reasons: ['stack frame match'], snippet: 'raise TimeoutError()', line_start: 1, line_end: 2 }], hypotheses: [{ rank: 1, title: 'Gateway timed out', explanation: 'The upstream request timed out.', confidence: .8, evidence: ['Timeout in logs'], next_checks: ['Check gateway latency'] }], summary: 'Payment gateway latency.', engine: 'deterministic' }, evidence: { raw_log: 'TimeoutError: gateway' }, error: null, connection_error: null };

function view(element: React.ReactNode, path = '/') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}>{element}</MemoryRouter></QueryClientProvider>);
}

beforeEach(() => { vi.restoreAllMocks(); });
function mockFetch(handler: (url: string, init?: RequestInit) => unknown) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => new Response(JSON.stringify(handler(String(url), init)), { status: 200, headers: { 'Content-Type': 'application/json' } }));
}

describe('dashboard workflows', () => {
  it('submits repository, logs, and service hint without waiting for the diagnosis', async () => {
    const requests: { url: string; body: Record<string, unknown> }[] = [];
    mockFetch((url, init) => { if (url.includes('/repositories')) return [repo]; requests.push({ url, body: JSON.parse(String(init?.body)) }); return { ...run, status: 'queued' }; });
    view(<Routes><Route path="/" element={<NewIncident />} /><Route path="/incidents/:id" element={<p>Saved incident</p>} /></Routes>);
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText(/Repository profile/), 'repo');
    await user.type(screen.getByLabelText('Incident logs'), 'TimeoutError: gateway');
    await user.type(screen.getByLabelText(/Service hint/), 'payments');
    await user.click(screen.getByRole('button', { name: 'Run analysis' }));
    await screen.findByText('Saved incident');
    expect(requests[0].body).toMatchObject({ repository_id: 'repo', log_text: 'TimeoutError: gateway', user_hint_service: 'payments' });
  });

  it('passes incident filters to the API', async () => {
    const fetch = mockFetch(url => url.includes('/repositories') ? [repo] : { items: [run], total: 1, page: 1, page_size: 20 });
    view(<Incidents />);
    await screen.findByText('Payment timeout');
    await userEvent.selectOptions(screen.getByLabelText('Filter status'), 'failed');
    await waitFor(() => expect(fetch.mock.calls.some(([url]) => String(url).includes('status=failed'))).toBe(true));
  });

  it('shows evidence, uncalibrated confidence, and original logs in distinct views', async () => {
    mockFetch(() => run);
    view(<Routes><Route path="/incidents/:id" element={<IncidentDetail />} /></Routes>, '/incidents/run-123');
    await screen.findByText('Gateway timed out');
    expect(screen.queryByText('80%')).not.toBeInTheDocument();
    expect(screen.getByText('Uncalibrated score; diagnosis probability unavailable')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('tab', { name: /Source evidence/ }));
    expect(await screen.findByText('raise TimeoutError()')).toBeInTheDocument();
    expect(screen.getByText('stack frame match')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('tab', { name: 'Logs & trace' }));
    expect(screen.getByText('TimeoutError: gateway')).toBeInTheDocument();
  });

  it('shows static graph provenance and source relationships in the evidence view', async () => {
    mockFetch(() => ({ ...run, evidence: { ...run.evidence, graph_context: {
      provider: 'gitnexus', provider_version: '1.6.12', repository: '/repos/commerce', repo_revision: 'abc123', indexed_revision: 'abc123',
      status: 'partial', reason: null, completeness: 'lower_bound', retrieval_ms: 12,
      symbols: [{ uid: 'checkout', name: 'checkout', file_path: 'wrapper.py', line_start: 3 }, { uid: 'charge', name: 'charge', file_path: 'gateway.py', line_start: 1 }],
      relationships: [{ source_uid: 'checkout', target_uid: 'charge', relationship: 'CALLS' }], seeds: [],
    } } }));
    view(<Routes><Route path="/incidents/:id" element={<IncidentDetail />} /></Routes>, '/incidents/run-123');
    await screen.findByText('Gateway timed out');
    await userEvent.click(screen.getByRole('tab', { name: /Source evidence/ }));
    expect(screen.getByText('Static code graph')).toBeInTheDocument();
    expect(screen.getByText(/checkout.*wrapper.py:3/)).toBeInTheDocument();
    expect(screen.getByText('Revision: abc123')).toBeInTheDocument();
    expect(screen.getByText(/runtime execution is unproven/)).toBeInTheDocument();
  });

  it.each(['unavailable', 'uncalibrated'] as const)('hides %s diagnosis scores and keeps tentative leads', async confidence_status => {
    mockFetch(() => ({ ...run, report: { ...run.report, diagnosis_status: 'insufficient_evidence',
      diagnosis_reason: 'Runtime input is missing.', next_checks: ['Capture the failing input'],
      hypotheses: [{ ...run.report!.hypotheses[0], confidence: confidence_status === 'unavailable' ? null : .8,
        confidence_status, model_score: .8 }] } }));
    const { container } = view(<Routes><Route path="/incidents/:id" element={<IncidentDetail />} /></Routes>, '/incidents/run-123');
    expect(await screen.findByText('Insufficient evidence')).toBeInTheDocument();
    expect(screen.getByText('Runtime input is missing.')).toBeInTheDocument();
    expect(screen.getByText('Capture the failing input')).toBeInTheDocument();
    expect(screen.getByText('Gateway timed out')).toBeInTheDocument();
    expect(screen.getByText('api/main.py: 24')).toBeInTheDocument();
    expect(screen.queryByText('80%')).not.toBeInTheDocument();
    expect(container.querySelector('.confidence-track')).toBeNull();
  });

  it('shows an abstention without findings as a completed report', async () => {
    mockFetch(() => ({ ...run, report: { ...run.report, diagnosis_status: 'insufficient_evidence',
      hypotheses: [], next_checks: ['Provide a fuller stack trace'] } }));
    view(<Routes><Route path="/incidents/:id" element={<IncidentDetail />} /></Routes>, '/incidents/run-123');
    expect(await screen.findByText('Insufficient evidence')).toBeInTheDocument();
    expect(screen.getByText('Provide a fuller stack trace')).toBeInTheDocument();
    expect(screen.getByText('0 findings')).toBeInTheDocument();
    expect(screen.queryByText('Investigation in progress')).not.toBeInTheDocument();
  });

  it('displays percentages only for explicitly calibrated confidence', async () => {
    mockFetch(() => ({ ...run, report: { ...run.report, diagnosis_status: 'provisional',
      hypotheses: [{ ...run.report!.hypotheses[0], confidence_status: 'calibrated' }] } }));
    const { container } = view(<Routes><Route path="/incidents/:id" element={<IncidentDetail />} /></Routes>, '/incidents/run-123');
    expect(await screen.findByText('80%')).toBeInTheDocument();
    expect(screen.getByText('diagnosis confidence (calibrated)')).toBeInTheDocument();
    expect(container.querySelector('.confidence-track')).not.toBeNull();
  });

  it('links findings to retrieved evidence and displays investigation gaps and actions', async () => {
    const item = { id: 'ev-path', kind: 'source', repository: '/repo', revision: 'abc123', file_path: 'cache.py', line_start: 4, line_end: 5, content: 'def _path(): return key', data: {}, limitations: ['lower_bound'], request_id: 'req-path', truncated: true };
    mockFetch(() => ({ ...run, report: { ...run.report, hypotheses: [{ ...run.report!.hypotheses[0], evidence_refs: ['ev-path'] }], retrieved_evidence: [item], investigation: { mode: 'agentic', version: 1, rounds: 2, model_calls: 3, tool_calls: 2, elapsed_ms: 1250, stop_reason: 'model_finished', incomplete: true, unresolved_gaps: ['Need the failing cache key'] }, investigation_events: [{ id: 'event-path', kind: 'retrieval', tool: 'read_source', question: 'How is the path built?', status: 'ok', evidence_ids: ['ev-path'], duration_ms: 10 }] } }));
    view(<Routes><Route path="/incidents/:id" element={<IncidentDetail />} /></Routes>, '/incidents/run-123');
    expect(await screen.findByText('Evidence investigation')).toBeInTheDocument();
    expect(screen.getByText('Incomplete')).toBeInTheDocument();
    expect(screen.getByText('Need the failing cache key')).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: 'ev-path' })[0]).toHaveAttribute('href', '#retrieved-evidence-ev-path');
    await userEvent.click(screen.getByText('Investigation actions (1)'));
    expect(screen.getByText('How is the path built?')).toBeVisible();
    await userEvent.click(screen.getByText(/cache.py:4/));
    expect(screen.getByText('def _path(): return key')).toBeVisible();
    expect(screen.getByText('Limitations: lower_bound')).toBeVisible();
    expect(screen.getByText(/Content clipped/)).toBeVisible();
  });

  it('shows actual retry attempts in the progress view', () => {
    view(<Pipeline run={{ ...run, status: 'running', stage: 'indexing', timeline: [{ stage: 'indexing', started_at: new Date().toISOString(), completed_at: null, attempt: 2 }] }} />);
    expect(screen.getByText('Attempt 2')).toBeInTheDocument();
    expect(screen.getByText('Repository indexing')).toBeInTheDocument();
  });

  it.each(['running', 'completed'] as const)('shows typed validation errors for a %s analysis', async status => {
    const errors = [{ stage: 'investigation_assessment', kind: 'schema', model_call: 2, total_issues: 1,
      issues: [{ path: ['hypotheses', 0, 'confidence'], code: 'float_type', message: 'Expected a number.' }] }];
    mockFetch(() => ({ ...run, status, report: status === 'completed' ? { ...run.report, engine: 'deterministic+llm', validation_errors: errors } : null,
      evidence: { ...run.evidence, validation_errors: errors } }));
    view(<Routes><Route path="/incidents/:id" element={<IncidentDetail />} /></Routes>, '/incidents/run-123');
    const alert = await screen.findByRole('alert', { name: 'Analysis validation errors' });
    expect(alert).toHaveTextContent('hypotheses.0.confidence');
    expect(alert).toHaveTextContent('float_type');
    expect(alert).toHaveTextContent('Expected a number.');
    expect(alert).toHaveTextContent('Model call 2');
    await userEvent.click(screen.getByRole('tab', { name: 'Logs & trace' }));
    expect(alert).toBeVisible();
  });

  it('refreshes active progress and stops polling after completion', async () => {
    let calls = 0;
    mockFetch(() => { calls++; return calls === 1 ? { ...run, status: 'queued', stage: 'queued', report: null } : run; });
    view(<Routes><Route path="/incidents/:id" element={<IncidentDetail />} /></Routes>, '/incidents/run-123');
    await screen.findByText('Queued for the worker');
    await screen.findByText('Gateway timed out', {}, { timeout: 4000 });
    const completedCalls = calls;
    await new Promise(resolve => setTimeout(resolve, 2100));
    expect(calls).toBe(completedCalls);
  });

  it('saves repository paths and validates access using the worker endpoint', async () => {
    const requests: string[] = [];
    mockFetch((url, init) => { requests.push(`${init?.method || 'GET'} ${url}`); if (url.endsWith('/validate')) return { valid: true, message: 'Repository is accessible from the worker.', services: [] }; if (init?.method === 'POST') return repo; return [repo]; });
    view(<Repositories />);
    await userEvent.click(await screen.findByRole('button', { name: 'Add repository' }));
    await userEvent.type(screen.getByLabelText('Profile name'), 'Commerce');
    await userEvent.type(screen.getByLabelText('Absolute repository path on worker'), '/repos/commerce');
    await userEvent.click(screen.getByRole('button', { name: 'Save profile' }));
    await screen.findByText(/Repository profile saved/);
    await userEvent.click(screen.getByRole('button', { name: 'Validate access' }));
    await screen.findByText('Repository is accessible from the worker.');
    expect(requests).toContain('POST /api/repositories/repo/validate');
  });

  it('keeps the form visible when saving configuration fails', async () => {
    mockFetch(() => ({ saved: { top_k_candidates: 10 }, active: { top_k_candidates: 10 }, overridden: [], restart_required: [], infrastructure_fields: [], control_db_path: '/data/control.db', secret_configured: false }));
    // API errors are also rendered consistently for request failures without losing the form.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => init?.method === 'PUT'
      ? new Response(JSON.stringify({ detail: 'Deep analysis count cannot exceed the candidate limit.' }), { status: 422 })
      : new Response(JSON.stringify({ saved: { llm_provider: 'ollama', top_k_candidates: 10, deep_analysis_top_n: 5, app_name: 'copilot', env: 'dev', ollama_base_url: 'http://localhost', ollama_model: 'model', openai_compatible_base_url: 'http://localhost/v1', openai_compatible_model: 'model', openai_compatible_api_key: '', temporal_host: 'localhost:7233', temporal_namespace: 'default', temporal_task_queue: 'copilot', temporal_connect_max_attempts: 60, temporal_connect_retry_delay_seconds: 2, data_dir: '/data', index_db_path: '/data/index.db' }, active: {}, overridden: [], restart_required: [], infrastructure_fields: [], control_db_path: '/data/control.db', secret_configured: false }), { status: 200 }));
    view(<Settings />);
    const input = await screen.findByLabelText('Deep analysis candidates');
    await userEvent.clear(input); await userEvent.type(input, '20');
    await userEvent.click(screen.getAllByRole('button', { name: 'Save changes' })[0]);
    expect(await screen.findByRole('alert')).toHaveTextContent('Deep analysis count cannot exceed');
    expect(input).toHaveValue(20);
  });

  it('saves only changed settings and explains pending infrastructure changes', async () => {
    const saved = { llm_provider: 'ollama', ollama_base_url: 'http://localhost:11434', ollama_model: 'model', openai_compatible_base_url: 'http://localhost:8000/v1', openai_compatible_model: 'model', openai_compatible_api_key: '', top_k_candidates: 10, deep_analysis_top_n: 5, temporal_host: 'new:7233', temporal_namespace: 'default', temporal_task_queue: 'copilot', temporal_connect_max_attempts: 60, temporal_connect_retry_delay_seconds: 2, app_name: 'copilot', env: 'dev', data_dir: '/data', index_db_path: '/data/index.db' };
    const config: SettingsData = { saved, active: { ...saved, temporal_host: 'old:7233' }, overridden: [], restart_required: ['temporal_host'], infrastructure_fields: ['temporal_host'], control_db_path: '/data/control.db', secret_configured: true };
    const requests: unknown[] = [];
    mockFetch((_url, init) => { if (init?.method === 'PUT') requests.push(JSON.parse(String(init.body))); return config; });
    view(<Settings />);
    const input = await screen.findByLabelText('Maximum candidates');
    await userEvent.clear(input); await userEvent.type(input, '12');
    await userEvent.click(screen.getAllByRole('button', { name: 'Save changes' })[0]);
    await waitFor(() => expect(requests).toEqual([{ values: { top_k_candidates: 12 }, reset: [] }]));
    expect(screen.getByText(/old:7233/)).toBeInTheDocument();
  });

  it('turns network failures into a useful reconnect message', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'));
    await expect(api('/overview')).rejects.toThrow('Cannot reach the local API');
  });

  it('saves iterative investigation as a boolean with bounded controls', async () => {
    const saved = { llm_provider: 'ollama', ollama_base_url: 'http://localhost:11434', ollama_model: 'model', openai_compatible_base_url: 'http://localhost:8000/v1', openai_compatible_model: 'model', openai_compatible_api_key: '', top_k_candidates: 10, deep_analysis_top_n: 5, temporal_host: 'localhost:7233', temporal_namespace: 'default', temporal_task_queue: 'copilot', temporal_connect_max_attempts: 60, temporal_connect_retry_delay_seconds: 2, app_name: 'copilot', env: 'dev', data_dir: '/data', index_db_path: '/data/index.db', agentic_enabled: false, agentic_max_rounds: 3, agentic_max_tool_requests: 12, agentic_timeout_seconds: 180 };
    const config: SettingsData = { saved, active: saved, overridden: [], restart_required: [], infrastructure_fields: [], control_db_path: '/data/control.db', secret_configured: false };
    const requests: unknown[] = [];
    mockFetch((_url, init) => { if (init?.method === 'PUT') requests.push(JSON.parse(String(init.body))); return config; });
    view(<Settings />);
    const enabled = await screen.findByLabelText('Iterative evidence retrieval');
    expect(enabled).toHaveValue('false');
    expect(screen.getByLabelText('Maximum retrieval rounds')).toHaveAttribute('max', '3');
    expect(screen.getByLabelText('Maximum evidence requests')).toHaveAttribute('max', '12');
    expect(screen.getByLabelText('Investigation deadline (seconds)')).toHaveAttribute('max', '180');
    await userEvent.selectOptions(enabled, 'true');
    await userEvent.click(screen.getAllByRole('button', { name: 'Save changes' })[0]);
    await waitFor(() => expect(requests).toEqual([{ values: { agentic_enabled: true }, reset: [] }]));
  });
});
