import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { HypothesisRecommendations, RunbookSnapshots } from './Recommendations';
import type { ActionEvent, Hypothesis, RunbookSnapshot } from './types';

const hypothesis: Hypothesis = {
  id: 'hypothesis-1', rank: 1, title: 'Dependency timeout', explanation: 'Observed request timeout',
  confidence: null, evidence: ['Request timed out'], next_checks: ['Compare latency'],
  action_plan: { version: 1,
    supporting_evidence: [{ claim: 'Request timed out in the client', evidence_refs: ['observed'] }],
    missing_evidence: ['Request-linked dependency latency is unavailable'],
    checks: [{ id: 'hypothesis-1-checks-1', instruction: 'Compare request-linked latency with the timeout',
      context: 'Read-only metrics for example-api in the incident environment', command: 'inspect <service>',
      placeholders: ['<service>: confirm the affected service'], evidence_to_collect: 'Dependency duration and configured timeout',
      supports_hypothesis: 'Linked duration exceeds the timeout', weakens_hypothesis: 'Linked duration is below the timeout' },
      { id: 'hypothesis-1-checks-2', instruction: 'Check another request', context: 'Same environment', command: null,
        placeholders: [], evidence_to_collect: 'Another linked duration', supports_hypothesis: 'The pattern repeats', weakens_hypothesis: 'The pattern does not repeat' }],
    runbooks: [], mitigations: [{ id: 'hypothesis-1-mitigations-1', title: 'Restore a known-good timeout', intended_effect: 'Correct the setting',
      prerequisites: ['Confirm the active value'], missing_prerequisites: ['Previous known-good value is unknown'], risks: ['Resource use may increase'],
      affected_services: ['example-api'], steps: ['Use the documented deployment process'], rollback: ['Restore the pre-change value'], runbook_ids: [],
      verification: { measure: 'Timeout rate and latency', success_condition: 'Meet agreed service objectives', observation_period: 'Unknown: agree before acting' } }],
  },
};
const evidence = [{ id: 'observed', kind: 'source', repository: '/repo', revision: 'abc', file_path: 'client.py', line_start: 12,
  content: 'timeout = 2', data: {}, limitations: [], truncated: false }];
let history: ActionEvent[];
let posts: Record<string, unknown>[];
function view() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><HypothesisRecommendations hypothesis={hypothesis} incidentId="incident" evidence={evidence} /></QueryClientProvider>);
}
beforeEach(() => {
  vi.restoreAllMocks(); history = []; posts = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => {
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body)); posts.push(body);
      const event = { ...body, incident_id: 'incident', action_kind: body.action_id.includes('checks') ? 'check' : 'mitigation', recorded_at: '2026-11-07T10:00:00Z' } as ActionEvent;
      history.push(event);
      return new Response(JSON.stringify(event), { status: 201 });
    }
    return new Response(JSON.stringify(history), { status: 200 });
  });
});

it('shows cited evidence, gaps, distinguishing predictions and a primary expanded check', () => {
  view();
  expect(screen.getByRole('link', { name: 'client.py:12' })).toHaveAttribute('href', '#retrieved-evidence-observed');
  expect(screen.getByText('Request-linked dependency latency is unavailable')).toBeInTheDocument();
  expect(screen.getByText('Linked duration exceeds the timeout')).toBeInTheDocument();
  expect(screen.getByText('Linked duration is below the timeout')).toBeInTheDocument();
  expect(screen.getByText('1. Compare request-linked latency with the timeout').parentElement).toHaveAttribute('open');
  expect(screen.getByText('2. Check another request').parentElement).not.toHaveAttribute('open');
  expect(screen.getByText('No matching runbook.')).toBeInTheDocument();
  expect(screen.getByText('Unreviewed model proposal')).toBeInTheDocument();
  expect(screen.getByText('Blocked: missing prerequisites')).toBeInTheDocument();
  expect(screen.getByText('Unknown: agree before acting')).toBeInTheDocument();
});

it('records check outcomes and preserves history on refresh', async () => {
  const initial = view();
  const region = screen.getByRole('region', { name: 'Outcome for hypothesis-1-checks-1' });
  fireEvent.click(within(region).getByText('Record outcome'));
  fireEvent.change(within(region).getByLabelText('Status'), { target: { value: 'supports' } });
  fireEvent.change(within(region).getByLabelText('Observations'), { target: { value: 'Duration exceeded the timeout' } });
  fireEvent.change(within(region).getByLabelText('Was this recommendation useful?'), { target: { value: 'true' } });
  fireEvent.click(within(region).getByRole('button', { name: 'Save outcome' }));
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0]).toMatchObject({ action_id: 'hypothesis-1-checks-1', status: 'supports', useful: true, observations: 'Duration exceeded the timeout' });
  await within(region).findByText('Responder outcome: supports');
  initial.unmount(); view();
  expect(await screen.findByText('Responder outcome: supports')).toBeInTheDocument();
});

it('requires recovery observations and keeps applied separate from verified', async () => {
  view();
  const region = screen.getByRole('region', { name: 'Outcome for hypothesis-1-mitigations-1' });
  fireEvent.click(within(region).getByText('Record outcome'));
  fireEvent.change(within(region).getByLabelText('Status'), { target: { value: 'verified' } });
  expect(within(region).getByRole('button', { name: 'Save outcome' })).toBeDisabled();
  fireEvent.change(within(region).getByLabelText('Observations (required)'), { target: { value: 'Measured timeout rate meets the agreed objective over the agreed period' } });
  fireEvent.click(within(region).getByRole('button', { name: 'Save outcome' }));
  await within(region).findByText('Responder outcome: verified');
  expect(posts[0]).toMatchObject({ status: 'verified' });
});

it('reuses the client event identifier when retrying a failed submission', async () => {
  const original = vi.mocked(globalThis.fetch).getMockImplementation()!;
  const attempted: Record<string, unknown>[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
    if (init?.method === 'POST') {
      attempted.push(JSON.parse(String(init.body)));
      if (attempted.length === 1) throw new Error('Network unavailable');
    }
    return original(url, init);
  });
  view();
  const region = screen.getByRole('region', { name: 'Outcome for hypothesis-1-checks-1' });
  fireEvent.click(within(region).getByText('Record outcome'));
  fireEvent.click(within(region).getByRole('button', { name: 'Save outcome' }));
  await within(region).findByText(/Cannot reach the local API/);
  fireEvent.click(within(region).getByRole('button', { name: 'Save outcome' }));
  await waitFor(() => expect(attempted).toHaveLength(2));
  expect(attempted[0].client_event_id).toBe(attempted[1].client_event_id);
});

it('renders repository snapshots as text and retains the historical view', () => {
  const book: RunbookSnapshot = { id: 'rb', title: 'Runbook', file_path: 'docs/runbooks/rb.md', revision: 'abc', content_hash: 'hash', services: ['api'],
    error_classes: [], failure_layers: [], sections: [{ heading: 'Diagnosis', line_start: 10, line_end: 12, content: '<img src=x onerror=alert(1)>' }] };
  const result = render(<RunbookSnapshots runbooks={[book]} />);
  expect(result.container.querySelector('img')).toBeNull();
  expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument();
  result.unmount();
  render(<HypothesisRecommendations hypothesis={{ ...hypothesis, action_plan: undefined }} incidentId="incident" evidence={[]} />);
  expect(screen.getByText('Compare latency')).toBeInTheDocument();
});
