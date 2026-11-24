import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, expect, it, vi } from 'vitest';
import { Repositories } from './pages/Repositories';
import { NewIncident } from './pages/Incidents';
import type { Repository } from './types';

const remote: Repository = { id: 'remote', name: 'GitHub app', source_type: 'github', repo_root: null,
  remote_url: 'https://github.com/acme/app.git', branch: 'main', compose_path: null, token_configured: true,
  archived: false, validation: null, index_summary: null, created_at: '2026-10-29T09:00:00Z' };

function view(element: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter>{element}</MemoryRouter></QueryClientProvider>);
}

function mockFetch(handler: (url: string, init?: RequestInit) => unknown) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => new Response(JSON.stringify(handler(String(url), init)), { status: 200, headers: { 'Content-Type': 'application/json' } }));
}

beforeEach(() => vi.restoreAllMocks());

it('saves a GitHub profile without automatically syncing or displaying its token', async () => {
  const writes: { url: string; body: Record<string, unknown> }[] = [];
  let saved = false;
  mockFetch((url, init) => {
    if (init?.method === 'POST') { writes.push({ url, body: JSON.parse(String(init.body)) }); saved = true; return remote; }
    return url.endsWith('/sync') ? { repository_id: 'remote', status: 'idle' } : saved ? [remote] : [];
  });
  view(<Repositories />);
  await userEvent.click(await screen.findByRole('button', { name: 'Add repository' }));
  await userEvent.selectOptions(screen.getByLabelText('Repository source'), 'github');
  expect(screen.queryByLabelText('Absolute repository path on worker')).not.toBeInTheDocument();
  for (const [label, value] of [['Profile name', 'GitHub app'], ['GitHub repository URL', remote.remote_url!], ['Tracked branch', 'main']]) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
  }
  fireEvent.change(screen.getByLabelText(/GitHub token/), { target: { value: 'github_pat_test' } });
  await userEvent.click(screen.getByRole('button', { name: 'Save profile' }));
  await screen.findByText(/Use Check & sync to download/);
  expect(writes).toEqual([{ url: '/api/repositories', body: expect.objectContaining({ source_type: 'github', remote_url: remote.remote_url, branch: 'main', github_token: 'github_pat_test' }) }]);
  expect(writes[0].body).not.toHaveProperty('repo_root');
  expect(screen.queryByDisplayValue('github_pat_test')).not.toBeInTheDocument();
});

it('only queues sync after a click and displays progress', async () => {
  const writes: string[] = [];
  mockFetch((url, init) => {
    if (init?.method === 'POST') { writes.push(url); return { id: 'job', repository_id: 'remote', status: 'running', stage: 'fetching' }; }
    return url.endsWith('/sync') ? { repository_id: 'remote', status: 'idle' } : [remote];
  });
  view(<Repositories />);
  const button = await screen.findByRole('button', { name: 'Check & sync' });
  await waitFor(() => expect(button).toBeEnabled());
  expect(writes).toEqual([]);
  await userEvent.click(button);
  await screen.findByText('Downloading changes');
  expect(screen.getByRole('button', { name: 'Syncing…' })).toBeDisabled();
  expect(writes).toEqual(['/api/repositories/remote/sync']);
});

it('requires initial sync before analysis and previews the synced commit afterwards', async () => {
  mockFetch(() => [remote]);
  const mounted = view(<NewIncident />);
  await userEvent.selectOptions(await screen.findByLabelText(/Repository profile/), 'remote');
  fireEvent.change(screen.getByLabelText('Incident logs'), { target: { value: 'error' } });
  expect(screen.getByRole('button', { name: 'Run analysis' })).toBeDisabled();
  expect(screen.getByText(/before starting an analysis/)).toBeInTheDocument();
  mounted.unmount();
  mockFetch(() => [{ ...remote, synced_revision: 'a'.repeat(40) }]);
  view(<NewIncident />);
  await userEvent.selectOptions(await screen.findByLabelText(/Repository profile/), 'remote');
  fireEvent.change(screen.getByLabelText('Incident logs'), { target: { value: 'error' } });
  expect(screen.getByText(/aaaaaaaaaaaa/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Run analysis' })).toBeEnabled();
});

it('retains the last synced commit when a later check fails and allows retry', async () => {
  mockFetch(url => url.endsWith('/sync') ? { id: 'job', repository_id: 'remote', status: 'failed', stage: 'failed', error: 'GitHub access failed.' }
    : url.endsWith('/graph') ? { status: 'disabled' } : [{ ...remote, synced_revision: 'b'.repeat(40) }]);
  view(<Repositories />);
  await screen.findByText('GitHub access failed.');
  expect(screen.getByText('Synced commit: bbbbbbbbbbbb')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Check & sync' })).toBeEnabled();
});

it('keeps saved tokens out of edit fields and offers explicit removal', async () => {
  const bodies: unknown[] = [];
  mockFetch((url, init) => {
    if (init?.method === 'PUT') { bodies.push(JSON.parse(String(init.body))); return remote; }
    return url.endsWith('/sync') ? { repository_id: 'remote', status: 'idle' } : [remote];
  });
  view(<Repositories />);
  await userEvent.click(await screen.findByRole('button', { name: 'Edit GitHub app' }));
  expect(screen.getByLabelText(/GitHub token optional/)).toHaveValue('');
  await userEvent.click(screen.getByRole('checkbox', { name: 'Remove saved GitHub token' }));
  await userEvent.click(screen.getByRole('button', { name: 'Save profile' }));
  await waitFor(() => expect(bodies).toEqual([expect.objectContaining({ clear_github_token: true })]));
});

it('cancels repository edits without writing and restores focus to the edit action', async () => {
  const fetch = mockFetch(url => url.endsWith('/sync') ? { repository_id: 'remote', status: 'idle' } : [remote]);
  view(<Repositories />);
  const edit = await screen.findByRole('button', { name: 'Edit GitHub app' });
  await userEvent.click(edit);
  expect(screen.getByLabelText('Profile name')).toHaveFocus();
  fireEvent.change(screen.getByLabelText('Profile name'), { target: { value: 'Unsaved name' } });
  await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(screen.queryByRole('form')).not.toBeInTheDocument();
  await waitFor(() => expect(edit).toHaveFocus());
  expect(fetch.mock.calls.every(([, init]) => !init?.method)).toBe(true);
  await userEvent.click(edit);
  expect(screen.getByLabelText('Profile name')).toHaveValue('GitHub app');
});

it('retains repository edits and shows save errors inside the form', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
    if (init?.method === 'PUT') return new Response(JSON.stringify({ detail: 'Repository profile could not be saved.' }), { status: 422 });
    return new Response(JSON.stringify(String(url).endsWith('/sync') ? { status: 'idle' } : [remote]), { status: 200 });
  });
  view(<Repositories />);
  await userEvent.click(await screen.findByRole('button', { name: 'Edit GitHub app' }));
  fireEvent.change(screen.getByLabelText('Profile name'), { target: { value: 'Updated name' } });
  await userEvent.click(screen.getByRole('button', { name: 'Save profile' }));
  const error = await screen.findByRole('alert');
  expect(error).toHaveTextContent('Repository profile could not be saved.');
  expect(screen.getByRole('form', { name: 'Edit repository profile' })).toContainElement(error);
  expect(screen.getByLabelText('Profile name')).toHaveValue('Updated name');
  expect(screen.getByRole('button', { name: 'Save profile' })).toBeEnabled();
});
