import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, expect, it, vi } from 'vitest';
import { LogSources } from './pages/LogSources';

const source = { collector_id: 'host-a', project: 'app', repository_id: 'repo', enabled: true, environment: 'production', services: ['api', 'db'], versioned_services: ['api'], last_seen: '2026-11-24T10:00:00Z', runtime_enabled: false, runtime_dependencies: { api: ['db'] }, telemetry_profile: 'prod' };
const repo = { id: 'repo', name: 'Commerce', archived: false };
function setup(initial = [source], repositories = [repo]) {
  let sources = initial;
  let failure = false;
  const writes: { url: string; body: Record<string, unknown> }[] = [];
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = String(input);
    let body: unknown;
    let status = 200;
    if (init?.method === 'PUT') {
      const update = JSON.parse(String(init.body));
      writes.push({ url, body: update });
      if (failure) { status = 422; body = { detail: 'Sync this repository before enabling collection' }; }
      else {
        const host = new URL(url, 'http://localhost').searchParams.get('collector_id');
        sources = sources.map(item => item.collector_id === host ? { ...item, ...update } : item);
        body = sources.find(item => item.collector_id === host);
      }
    } else if (url.includes('/repositories')) body = repositories;
    else if (url.includes('/deployments')) {
      const page = Number(new URL(url, 'http://localhost').searchParams.get('page'));
      body = { items: [{ deployment_id: 'd' + page, service: 'api', version: 'v' + page, container_id: 'container', revision: 'a'.repeat(40), first_observed: source.last_seen, last_observed: source.last_seen }], total: 2, page, page_size: 1 };
    } else body = sources;
    return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  });
  render(<QueryClientProvider client={client}><MemoryRouter><LogSources /></MemoryRouter></QueryClientProvider>);
  return { client, writes, fail: () => { failure = true; }, refresh: async (next: typeof initial) => {
    sources = next;
    await act(async () => { await client.invalidateQueries({ queryKey: ['log-sources'] }); });
  } };
}
beforeEach(() => vi.restoreAllMocks());

it('shows compact summaries and removes runtime configuration', async () => {
  setup();
  expect(await screen.findByText('Enabled')).toBeVisible();
  expect(screen.getByText('Commerce · production')).toBeVisible();
  expect(screen.getByText('Version matching: api')).toBeVisible();
  expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  expect(screen.queryByText(/Retrieve passive|Telemetry profile|Dependencies of|Capabilities:|Helper last seen/)).not.toBeInTheDocument();
  expect(screen.getByText('Source details').closest('details')).not.toHaveAttribute('open');
  await userEvent.click(screen.getByRole('button', { name: 'Configure app (host-a)' }));
  expect(screen.getByText('Version matching', { selector: 'summary' }).closest('details')).not.toHaveAttribute('open');
  expect(screen.queryByText('Runtime evidence')).not.toBeInTheDocument();
});

it('cancels edits and preserves unsaved values across source refreshes', async () => {
  const app = setup();
  await userEvent.click(await screen.findByRole('button', { name: 'Configure app (host-a)' }));
  fireEvent.change(screen.getByLabelText('Environment for app (host-a)'), { target: { value: 'staging' } });
  await app.refresh([{ ...source, environment: 'updated remotely' }]);
  expect(screen.getByLabelText('Environment for app (host-a)')).toHaveValue('staging');
  await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(app.writes).toHaveLength(0);
  await userEvent.click(screen.getByRole('button', { name: 'Configure app (host-a)' }));
  expect(screen.getByLabelText('Environment for app (host-a)')).toHaveValue('updated remotely');
});

it('saves for the correct collector, retains missing services, and omits hidden runtime settings', async () => {
  const app = setup([{ ...source, services: ['db'] }, { ...source, collector_id: 'host-b', enabled: false }]);
  expect(await screen.findByText('Disabled')).toBeVisible();
  const card = screen.getByRole('article', { name: 'Log source app (host-a)' });
  expect(within(card).getByText('Collector: host-a')).toBeVisible();
  await userEvent.click(within(card).getByRole('button', { name: 'Configure app (host-a)' }));
  fireEvent.change(screen.getByLabelText('Environment for app (host-a)'), { target: { value: 'staging' } });
  await userEvent.click(within(card).getByText('Version matching', { selector: 'summary' }));
  expect(within(card).getByRole('checkbox', { name: 'api (not running)' })).toBeChecked();
  await userEvent.click(within(card).getByRole('button', { name: 'Save changes' }));
  await waitFor(() => expect(screen.queryByRole('form')).not.toBeInTheDocument());
  expect(app.writes).toEqual([{ url: '/api/log-sources/app?collector_id=host-a', body: { repository_id: 'repo', enabled: true, environment: 'staging', versioned_services: ['api'] } }]);
  expect(within(card).getByText('Commerce · staging')).toBeVisible();
  expect(screen.getByText('Settings saved for app (host-a).')).toBeVisible();
});

it('keeps edits and displays validation errors after a failed save', async () => {
  const app = setup();
  app.fail();
  await userEvent.click(await screen.findByRole('button', { name: 'Configure app (host-a)' }));
  fireEvent.change(screen.getByLabelText('Environment for app (host-a)'), { target: { value: 'staging' } });
  await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Sync this repository');
  expect(screen.getByLabelText('Environment for app (host-a)')).toHaveValue('staging');
  expect(screen.getByRole('button', { name: 'Save changes' })).toBeEnabled();
});

it('handles missing repositories and zero services without enabling invalid collection', async () => {
  setup([{ ...source, repository_id: '', services: [], versioned_services: [] }], []);
  expect(await screen.findByText('Setup required')).toBeVisible();
  expect(screen.getByRole('link', { name: 'Add a repository' })).toBeVisible();
  expect(screen.getByText(/0 services/)).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: 'Configure app (host-a)' }));
  expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled();
  await userEvent.click(screen.getByRole('checkbox', { name: 'Collect errors' }));
  expect(screen.getByRole('button', { name: 'Save changes' })).toBeEnabled();
});

it('exposes container version history and pagination through source details', async () => {
  setup();
  await userEvent.click(await screen.findByText('Source details', { selector: 'summary' }));
  expect(screen.getByText('Services: api, db')).toBeVisible();
  await userEvent.click(screen.getByText('Observed container versions', { selector: 'summary' }));
  expect(await screen.findByText('api / v1')).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: 'Next versions' }));
  expect(await screen.findByText('api / v2')).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: 'Previous versions' }));
  expect(await screen.findByText('api / v1')).toBeVisible();
});
