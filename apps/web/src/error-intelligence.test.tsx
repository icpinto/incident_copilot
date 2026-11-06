import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import type { ErrorIntelligenceData, ErrorOccurrence, Issue, IssueActivity } from './types';

const now = '2026-11-05T12:00:00Z';
const initialIssue: Issue = {
  id: 'issue-1', title: 'ValueError: charge failed', repository_id: 'repo', repository_name: 'Commerce',
  owner: '', status: 'open', resolution_note: '', resolved_at: null, created_at: now, updated_at: now,
  first_seen: now, last_seen: now, occurrence_count: 2, previous_count: 1, total_count: 2,
  change: 1, change_percent: 100, source_timestamp_count: 1, fallback_timestamp_count: 1,
  tracking_since: now,
};
const sourceIssue: Issue = { ...initialIssue, id: 'issue-2', title: 'Other error' };
const occurrence = (id: string): ErrorOccurrence => ({
  id, issue_id: initialIssue.id, batch_id: 'batch', event_id: `event-${id}`, effective_at: now,
  timestamp_source: 'submission', investigations: [{ id: 'run-1', title: 'Checkout investigation' }],
  event: { id: `event-${id}`, line_start: 2, line_end: 2, message: 'ValueError: charge failed', service: 'api',
    timestamp: null, timestamp_utc: null, trace_id: 'trace-a', request_id: null, error_bearing: true,
    exceptions: [{ id: 'x1', type: 'ValueError', message: 'charge failed', relation: null, previous_id: null, stack_frames: [] }],
    stack_frames: [], limitations: ['Submission time used'], group_id: 'g1' },
});
const intelligence: ErrorIntelligenceData = {
  tracking_since: now, start_at: '2026-10-22T00:00:00Z', end_at: now, previous_start_at: '2026-10-08T00:00:00Z', dimension: 'service',
  summary: { occurrences: 2, previous_occurrences: 1, change: 1, change_percent: 100, issues: 1, active_issues: 1,
    new_issues: 1, reopened_issues: 0, source_timestamp_count: 1, fallback_timestamp_count: 1 },
  trends: [{ date: '2026-11-05', occurrences: 2 }], hotspots: [{ value: 'api', label: 'api', occurrences: 2 }],
  frequent_issues: [initialIssue], growing_issues: [initialIssue],
};
const clients: QueryClient[] = [];
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
let issue: Issue;
let activities: IssueActivity[];
let calls: { path: string; method: string; body: Record<string, unknown> }[];
function mockApi(empty = false) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = new URL(String(input), 'http://localhost');
    const method = init?.method || 'GET';
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    calls.push({ path: url.pathname, method, body });
    if (url.pathname === '/health') return response({ status: 'ok' });
    if (url.pathname === '/api/repositories') return response([{ id: 'repo', name: 'Commerce' }]);
    if (url.pathname === '/api/error-intelligence') return response(empty ? { ...intelligence, hotspots: [], frequent_issues: [], growing_issues: [], summary: { ...intelligence.summary, occurrences: 0 } } : intelligence);
    if (url.pathname === '/api/issues') return response({ items: empty ? [] : [issue, sourceIssue], total: empty ? 0 : 2, page: 1, page_size: 20, start_at: intelligence.start_at, end_at: now, tracking_since: now });
    if (url.pathname.endsWith('/occurrences')) return response({ items: [occurrence('o1'), occurrence('o2')], total: 2, page: 1, page_size: 20 });
    if (url.pathname.endsWith('/activity')) return response({ items: activities, total: activities.length, page: 1, page_size: 20 });
    if (url.pathname.endsWith('/merge')) { issue = { ...issue, total_count: 4 }; return response(issue); }
    if (url.pathname.endsWith('/split')) { issue = { ...issue, id: 'split-issue', title: 'Separated error', total_count: 1 }; return response(issue, 201); }
    if (url.pathname.startsWith('/api/issues/')) {
      if (method === 'PATCH') {
        issue = { ...issue, ...body } as Issue;
        activities = [{ id: 'a1', issue_id: issue.id, kind: 'updated', created_at: now, effective_at: now, details: { changes: { status: { from: 'open', to: issue.status } } } }];
      }
      return response(issue);
    }
    if (url.pathname === '/api/incidents/run-1') return response({ id: 'run-1', title: 'Checkout investigation', repository_id: 'repo', repository_name: 'Commerce', status: 'failed', created_at: now, updated_at: now, completed_at: now, config_revision: 'rev', stage: 'indexing', timeline: [], report: null, evidence: { raw_log: 'Started\nValueError: charge failed' }, error: null, connection_error: null, error_batch_id: 'batch', error_tracking_status: 'tracked' });
    throw new Error(`Unexpected request ${url}`);
  });
}
function Location() { const location = useLocation(); return <output aria-label="Current location">{location.pathname}{location.search}</output>; }
function view(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  clients.push(client);
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><App /><Location /></MemoryRouter></QueryClientProvider>);
}
beforeEach(() => { vi.restoreAllMocks(); issue = { ...initialIssue }; activities = []; calls = []; });
afterEach(() => { clients.splice(0).forEach(client => client.clear()); });

describe('uploaded error intelligence', () => {
  it('shows coverage and drills from a hotspot into a filtered issue list', async () => {
    mockApi(); view('/intelligence');
    expect(await screen.findByRole('heading', { name: 'Most frequent issues' })).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Error occurrences' })).getByText('2')).toBeInTheDocument();
    expect(screen.getByText(/Tracking new submissions since/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'api 2' }));
    expect(screen.getByLabelText('Current location')).toHaveTextContent('/issues?service_name=api');
    expect(await screen.findByRole('heading', { name: 'Recurring issues' })).toBeInTheDocument();
  });

  it('keeps shared filters in the URL and renders empty results', async () => {
    mockApi(true); view('/issues?owner=Payments&service_name=api');
    expect(await screen.findByText('No matching issues')).toBeInTheDocument();
    expect(screen.getByLabelText('Issue owner')).toHaveValue('Payments');
    await userEvent.selectOptions(screen.getByLabelText('Issue status filter'), 'resolved');
    expect(screen.getByLabelText('Current location')).toHaveTextContent('owner=Payments&service_name=api&status=resolved');
    await userEvent.click(screen.getByRole('button', { name: 'Reset filters' }));
    expect(screen.getByLabelText('Current location')).toHaveTextContent('/issues');
  });

  it('renders failed requests with a useful message', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => String(input).includes('error-intelligence') ? response({ detail: 'Cannot load error analytics' }, 503) : response([]));
    view('/intelligence');
    expect(await screen.findByRole('alert')).toHaveTextContent('Cannot load error analytics');
  });
});

describe('issue triage and evidence', () => {
  it('saves ownership, resolution and notes, then refreshes activity', async () => {
    mockApi(); view('/issues/issue-1');
    await screen.findByLabelText('Issue title');
    await userEvent.type(screen.getByLabelText('Issue owner'), 'Payments');
    await userEvent.selectOptions(screen.getByLabelText('Issue status'), 'resolved');
    await userEvent.type(screen.getByLabelText('Resolution note'), 'Fixed retries');
    await userEvent.click(screen.getByRole('button', { name: 'Save issue' }));
    await waitFor(() => expect(calls.find(call => call.method === 'PATCH')?.body).toMatchObject({ owner: 'Payments', status: 'resolved', resolution_note: 'Fixed retries' }));
    expect(await screen.findByText('Changes saved')).toBeInTheDocument();
    expect(await screen.findByText('status: open → resolved')).toBeInTheDocument();
  });

  it('splits selected occurrences and opens the resulting issue', async () => {
    mockApi(); view('/issues/issue-1');
    await userEvent.click(await screen.findByLabelText('Select occurrence o1'));
    await userEvent.click(screen.getByRole('button', { name: 'Split 1 selected occurrences' }));
    await waitFor(() => expect(screen.getByLabelText('Current location')).toHaveTextContent('/issues/split-issue'));
    expect(calls.find(call => call.path.endsWith('/split'))?.body).toEqual({ occurrence_ids: ['o1'] });
  });

  it('prevents splitting all occurrences into a new issue', async () => {
    mockApi(); view('/issues/issue-1');
    await userEvent.click(await screen.findByLabelText('Select occurrence o1'));
    await userEvent.click(screen.getByLabelText('Select occurrence o2'));
    expect(screen.getByRole('button', { name: 'Split 2 selected occurrences' })).toBeDisabled();
  });

  it('merges selected sources into the current issue', async () => {
    mockApi(); view('/issues/issue-1');
    await userEvent.click(await screen.findByRole('button', { name: 'Merge issues' }));
    await userEvent.click(await screen.findByRole('checkbox', { name: /Other error/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Merge 1 selected issues' }));
    await waitFor(() => expect(calls.find(call => call.path.endsWith('/merge'))?.body).toEqual({ source_issue_ids: ['issue-2'] }));
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Merge into this issue' })).not.toBeInTheDocument());
  });

  it('opens the precise original lines in an investigation', async () => {
    mockApi(); view('/issues/issue-1');
    const links = await screen.findAllByRole('link', { name: 'Checkout investigation · original lines 2–2' });
    await userEvent.click(links[0]);
    expect(await screen.findByText('Selected original lines 2–2')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Logs & trace' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('link', { name: 'View recurring issues from this upload' })).toHaveAttribute('href', '/issues?incident_id=run-1');
  });
});
