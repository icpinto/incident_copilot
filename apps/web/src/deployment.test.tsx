import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, expect, it, vi } from 'vitest';
import { DeployedVersion } from './Deployment';
import { LogSources } from './pages/LogSources';
import { NewIncident } from './pages/Incidents';
import type { DeploymentContext } from './types';

const context: DeploymentContext = { deployment_id: 'deployment-a', collector_id: 'host-a', project: 'app', service: 'api', container_id: 'container-a', environment: 'production', revision: 'a'.repeat(40), version: 'v1', image_ref: 'app:latest', image_id: 'sha256:config', repo_digests: ['app@sha256:manifest'], source_url: null };
const repo = { id: 'repo', name: 'App', source_type: 'github', remote_url: 'https://github.com/acme/app', branch: 'main', synced_revision: null, validation: null, archived: false };
const observed = { ...context, repository_id: 'repo', first_observed: '2026-11-06T10:00:00Z', last_observed: '2026-11-06T11:00:00Z' };
function view(element: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter>{element}</MemoryRouter></QueryClientProvider>);
}
function mockFetch(handler: (url: string, init?: RequestInit) => unknown) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => new Response(JSON.stringify(handler(String(url), init)), { status: 200, headers: { 'Content-Type': 'application/json' } }));
}
beforeEach(() => vi.restoreAllMocks());

it('shows declared and analyzed commits, image identities, and source failure independently', () => {
  view(<DeployedVersion context={context} resolution={{ status: 'unavailable', requested_commit: context.revision, analyzed_commit: null, reason: 'Commit is not available; push it and rerun.' }} />);
  expect(screen.getByText('a'.repeat(40))).toBeInTheDocument();
  expect(screen.getByText('sha256:config')).toBeInTheDocument();
  expect(screen.getByText('app@sha256:manifest')).toBeInTheDocument();
  expect(screen.getByRole('alert')).toHaveTextContent('push it and rerun');
  expect(screen.getByText('production / api')).toBeInTheDocument();
});

it('shows the saved runtime settings with redacted values and mount metadata', async () => {
  view(<DeployedVersion context={{ ...context, runtime_config: { status: 'available',
    environment: [{ name: 'LOG_LEVEL', value: 'debug' }, { name: 'DB_PASSWORD', value: '[redacted]' }],
    mounts: [{ destination: '/app/config', type: 'bind', read_only: true }], truncated: false } }} />);
  await userEvent.click(screen.getByText('Observed runtime configuration'));
  expect(screen.getByText('LOG_LEVEL')).toBeInTheDocument();
  expect(screen.getByText('debug')).toBeInTheDocument();
  expect(screen.getByText('[redacted]')).toBeInTheDocument();
  expect(screen.getByText('/app/config')).toBeInTheDocument();
});

it('saves environment and per-service enablement for the correct collector', async () => {
  const writes: { url: string; body: unknown }[] = [];
  mockFetch((url, init) => {
    if (init?.method === 'PUT') { writes.push({ url, body: JSON.parse(String(init.body)) }); return {}; }
    if (url.includes('/deployments')) return { items: [observed], total: 1, page: 1, page_size: 20 };
    if (url.includes('/repositories')) return [repo];
    return [{ collector_id: 'host-a', project: 'app', services: ['api', 'db'], repository_id: 'repo', environment: 'unknown', versioned_services: [], enabled: true, last_seen: observed.last_observed }];
  });
  view(<LogSources />);
  fireEvent.change(await screen.findByLabelText('Environment for app'), { target: { value: 'production' } });
  await userEvent.click(screen.getByRole('checkbox', { name: 'api' }));
  await userEvent.click(screen.getByRole('button', { name: 'Save source' }));
  await waitFor(() => expect(writes).toEqual([{ url: '/api/log-sources/app?collector_id=host-a', body: { repository_id: 'repo', enabled: true, environment: 'production', versioned_services: ['api'] } }]));
  expect(screen.getByRole('checkbox', { name: 'db' })).not.toBeChecked();
  expect(screen.getByText(/First observed/)).toBeInTheDocument();
});

it('selects an observed deployment before first branch sync and submits its identity', async () => {
  const writes: Record<string, unknown>[] = [];
  mockFetch((url, init) => {
    if (url.includes('/deployments')) return { items: [observed], total: 1, page: 1, page_size: 20 };
    if (url.includes('/repositories')) return [repo];
    if (init?.method === 'POST') { writes.push(JSON.parse(String(init.body))); return { id: 'saved' }; }
    return {};
  });
  view(<Routes><Route path="/" element={<NewIncident />} /><Route path="/incidents/:id" element={<p>Saved</p>} /></Routes>);
  await userEvent.selectOptions(await screen.findByLabelText(/Repository profile/), 'repo');
  expect(screen.getByRole('button', { name: 'Run analysis' })).toBeDisabled();
  await screen.findByRole('option', { name: /production \/ api \/ v1/ });
  await userEvent.selectOptions(screen.getByLabelText('Observed deployment'), 'deployment-a');
  fireEvent.change(screen.getByLabelText('Incident logs'), { target: { value: 'ValueError: failed' } });
  await userEvent.click(screen.getByRole('button', { name: 'Run analysis' }));
  await screen.findByText('Saved');
  expect(writes[0]).toMatchObject({ repository_id: 'repo', deployment_id: 'deployment-a', user_hint_service: 'api' });
});
