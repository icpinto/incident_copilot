import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import { App } from './App';
import { Overview } from './pages/Overview';
import { Incidents } from './pages/Incidents';
import type { Incident, OverviewData } from './types';

const repo = { id: 'repo', name: 'Commerce', source_type: 'local', repo_root: '/repos/commerce', archived: false, validation: null, index_summary: null };
const incident: Incident = {
  id: 'run-123', title: 'Payment timeout', repository_id: 'repo', repository_name: 'Commerce',
  status: 'completed', created_at: '2026-10-22T09:00:00Z', updated_at: '2026-10-22T09:01:00Z',
  completed_at: '2026-10-22T09:01:00Z', config_revision: 'rev1', stage: 'completed', timeline: [],
  report: null, evidence: {}, error: null, connection_error: null,
};
const emptyOverview: OverviewData = { total: 0, completed: 0, active: 0, failed: 0, average_duration_seconds: 0, activity: [], services: [], recent: [] };
const populatedOverview: OverviewData = {
  total: 12, completed: 9, active: 2, failed: 1, average_duration_seconds: 90,
  activity: [{ date: '2026-10-22', incidents: 3 }], services: [{ name: 'payments', count: 9 }], recent: [incident],
};
const clients: QueryClient[] = [];
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
function mockApi(overview = emptyOverview, repositories = [repo]) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
    const url = String(input);
    if (url === '/health') return response({ status: 'ok' });
    if (url === '/api/overview') return response(overview);
    if (url === '/api/repositories') return response(repositories);
    if (url.includes('/graph')) return response({ status: 'not_prepared' });
    if (url.startsWith('/api/incidents')) return response({ items: [incident], total: 41, page: 1, page_size: 20 });
    throw new Error(`Unexpected API call: ${url}`);
  });
}
function RouterProbe() {
  const location = useLocation();
  const navigate = useNavigate();
  return <><output aria-label="Current location">{location.pathname}{location.search}</output>
    <button onClick={() => navigate('/incidents?service_name=orders&page=3')}>Saved service view</button>
    <button onClick={() => navigate(-1)}>Go back</button></>;
}
function view(content: ReactNode, path = '/') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}>{content}<RouterProbe /></MemoryRouter></QueryClientProvider>);
}
beforeEach(() => { vi.restoreAllMocks(); });
afterEach(() => { clients.splice(0).forEach(client => client.clear()); });

describe('dashboard overview states', () => {
  it('renders saved metrics and links to the latest investigation', async () => {
    mockApi(populatedOverview);
    view(<Overview />);
    expect(await screen.findByText('2 investigations in progress')).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Total incidents' })).getByText('12')).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Completed analyses' })).getByText('75% of submitted incidents')).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Avg. diagnosis time' })).getByText('1m 30s')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Payment timeout' })).toHaveAttribute('href', '/incidents/run-123');
    expect(screen.getByRole('img', { name: 'payments: 9' })).toBeInTheDocument();
  });
  it('uses honest zero and unavailable metrics and guides repository onboarding', async () => {
    mockApi(emptyOverview, []);
    view(<Overview />);
    expect(await screen.findByText('Ready for your first investigation')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Connect a repository' })).toHaveAttribute('href', '/repositories');
    expect(within(screen.getByRole('region', { name: 'Avg. diagnosis time' })).getByText('—')).toBeInTheDocument();
    expect(screen.getByText('No incidents yet')).toBeInTheDocument();
    expect(screen.getByText('No service data yet')).toBeInTheDocument();
  });
  it('guides an empty workspace with a repository to a new analysis', async () => {
    mockApi();
    view(<Overview />);
    expect(await screen.findByRole('link', { name: 'Start an analysis' })).toHaveAttribute('href', '/incidents/new');
  });
  it('shows loading without displaying invented metrics', () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => new Promise(() => {}));
    view(<Overview />);
    expect(screen.getByRole('status', { name: 'Loading workspace' })).toHaveTextContent('Loading workspace');
    expect(screen.queryByRole('region', { name: 'Total incidents' })).not.toBeInTheDocument();
  });
  it('shows an API failure without an empty-success dashboard', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(response({ detail: 'Overview unavailable' }, 503));
    view(<Overview />);
    expect((await screen.findAllByRole('alert'))[0]).toHaveTextContent('Overview unavailable');
    expect(screen.queryByText('No incidents yet')).not.toBeInTheDocument();
  });
  it('reports repository lookup failures without claiming zero repositories', async () => {
    const fetch = mockApi();
    fetch.mockImplementation(async input => String(input) === '/api/overview' ? response(emptyOverview) : response({ detail: 'Repository lookup failed' }, 503));
    view(<Overview />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Repository lookup failed');
    expect(screen.getByText('Repository count unavailable')).toBeInTheDocument();
    expect(screen.queryByText('0 connected repositories')).not.toBeInTheDocument();
  });
});

describe('shared workspace navigation', () => {
  it('navigates with keyboard and marks the active section', async () => {
    mockApi();
    view(<App />);
    await screen.findByText('API connected');
    const incidents = within(screen.getByRole('navigation', { name: 'Main navigation' })).getByRole('link', { name: 'Incidents' });
    incidents.focus();
    await userEvent.keyboard('{Enter}');
    expect(await screen.findByRole('heading', { level: 1, name: 'Incidents' })).toBeInTheDocument();
    expect(incidents).toHaveAttribute('aria-current', 'page');
  });
  it('moves focus and selection between investigation tabs with arrow keys', async () => {
    const fetch = mockApi();
    fetch.mockImplementation(async input => String(input) === '/api/incidents/run-123' ? response(incident) : response({ status: 'ok' }));
    view(<App />, '/incidents/run-123');
    const diagnosis = await screen.findByRole('tab', { name: 'Diagnosis' });
    diagnosis.focus();
    await userEvent.keyboard('{ArrowRight}');
    const evidence = screen.getByRole('tab', { name: 'Source evidence (0)' });
    expect(evidence).toHaveFocus();
    expect(evidence).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', evidence.id);
    await userEvent.keyboard('{ArrowLeft}');
    expect(diagnosis).toHaveFocus();
    expect(diagnosis).toHaveAttribute('aria-selected', 'true');
  });
  it('submits and restores an encoded search through the URL', async () => {
    const fetch = mockApi();
    view(<App />, '/incidents?q=previous');
    const search = screen.getByRole('textbox', { name: 'Search incidents' });
    await waitFor(() => expect(search).toHaveValue('previous'));
    await userEvent.clear(search);
    await userEvent.type(search, 'payment & gateway{Enter}');
    expect(screen.getByLabelText('Current location')).toHaveTextContent('/incidents?q=payment%20%26%20gateway');
    expect(screen.getByRole('textbox', { name: 'Filter incidents' })).toHaveValue('payment & gateway');
    await waitFor(() => expect(fetch.mock.calls.some(([url]) => String(url).includes('q=payment+%26+gateway'))).toBe(true));
    await userEvent.click(screen.getByRole('button', { name: 'Go back' }));
    await waitFor(() => expect(search).toHaveValue('previous'));
  });
  it.each([503, 200])('reports offline for unhealthy responses (HTTP %s)', async status => {
    mockApi().mockImplementation(async input => String(input) === '/health' ? response({ status: 'unavailable' }, status) : response(String(input).includes('repositories') ? [] : emptyOverview));
    view(<App />);
    expect(await screen.findByText('API offline')).toBeInTheDocument();
    expect(screen.queryByText('API connected')).not.toBeInTheDocument();
  });
  it('keeps the initial connecting state distinct from connected', async () => {
    let resolveHealth!: (response: Response) => void;
    mockApi().mockImplementation(input => String(input) === '/health' ? new Promise(resolve => { resolveHealth = resolve; }) : Promise.resolve(response(String(input).includes('repositories') ? [] : emptyOverview)));
    view(<App />);
    expect(screen.getByText('Connecting')).toBeInTheDocument();
    expect(screen.queryByText('API connected')).not.toBeInTheDocument();
    await act(async () => resolveHealth(response({ status: 'ok' })));
    expect(await screen.findByText('API connected')).toBeInTheDocument();
  });
});

describe('advanced incident filters', () => {
  it('toggles secondary filters with keyboard and preserves their URL values', async () => {
    const fetch = mockApi();
    view(<Incidents />, '/incidents?page=3');
    const toggle = screen.getByRole('button', { name: 'More filters' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByLabelText('Filter service')).not.toBeVisible();
    toggle.focus();
    await userEvent.keyboard('{Enter}');
    expect(screen.getByLabelText('Filter service')).toBeVisible();
    await userEvent.type(screen.getByLabelText('Filter service'), 'payments');
    expect(screen.getByLabelText('Current location')).toHaveTextContent('/incidents?service_name=payments');
    await userEvent.click(toggle);
    expect(screen.getByLabelText('Filter service')).not.toBeVisible();
    await userEvent.click(toggle);
    expect(screen.getByLabelText('Filter service')).toHaveValue('payments');
    await waitFor(() => expect(fetch.mock.calls.some(([url]) => String(url).includes('service_name=payments'))).toBe(true));
    await userEvent.click(screen.getByRole('button', { name: 'Reset' }));
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByLabelText('Filter service')).toHaveValue('');
    expect(screen.getByLabelText('Current location').textContent).toBe('/incidents');
  });
  it('opens advanced filters for deep links and subsequent navigation', async () => {
    mockApi();
    view(<Incidents />, '/incidents?error_class=TimeoutError&start_date=2026-10-22');
    const toggle = screen.getByRole('button', { name: 'More filters' });
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByLabelText('Filter error class')).toHaveValue('TimeoutError');
    expect(screen.getByLabelText('Start date')).toHaveValue('2026-10-22');
    await userEvent.click(toggle);
    await userEvent.click(screen.getByRole('button', { name: 'Saved service view' }));
    await waitFor(() => expect(toggle).toHaveAttribute('aria-expanded', 'true'));
    expect(screen.getByLabelText('Filter service')).toHaveValue('orders');
  });
});
