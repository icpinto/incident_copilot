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

  it('shows evidence, confidence, and original logs in distinct views', async () => {
    mockFetch(() => run);
    view(<Routes><Route path="/incidents/:id" element={<IncidentDetail />} /></Routes>, '/incidents/run-123');
    await screen.findByText('Gateway timed out');
    expect(screen.getByText('80%')).toBeInTheDocument();
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

  it('shows actual retry attempts in the progress view', () => {
    view(<Pipeline run={{ ...run, status: 'running', stage: 'indexing', timeline: [{ stage: 'indexing', started_at: new Date().toISOString(), completed_at: null, attempt: 2 }] }} />);
    expect(screen.getByText('Attempt 2')).toBeInTheDocument();
    expect(screen.getByText('Repository indexing')).toBeInTheDocument();
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
});
