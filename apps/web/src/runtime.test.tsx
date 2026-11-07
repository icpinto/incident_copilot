import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it } from 'vitest';
import { RuntimeEvidence } from './RuntimeEvidence';
import { Investigation } from './Investigation';
import type { EvidenceItem } from './types';

const item: EvidenceItem = { id: 'rt-1', kind: 'runtime_resources', repository: null, revision: null,
  runtime: { collector_id: 'host', project: 'app', service: 'db', environment: 'prod', container_ids: ['original'],
    requested_start: '2026-11-07T10:00:00Z', requested_end: '2026-11-07T10:05:00Z', observed_at: '2026-11-07T10:05:10Z', coverage: 'partial' },
  data: { observations: [{ service: 'db', container_id: 'original', samples: [{ timestamp: '2026-11-07T10:04:00Z', cpu_percent: 75, memory_bytes: 1048576, memory_limit_bytes: 2097152 }] }] },
  content: '', limitations: ['Buffer warm-up'], truncated: true };

it('shows runtime collection gaps and resource history without source evidence', async () => {
  render(<RuntimeEvidence evidence={[item]} collection={[{ id: 'resources', capability: 'resources', status: 'partial', duration_ms: 12 }, { id: 'traces', capability: 'traces', status: 'unavailable', reason: 'tempo_not_configured', duration_ms: 1 }]} />);
  expect(screen.getByText('Runtime collection')).toBeInTheDocument();
  expect(screen.getByText(/tempo_not_configured/)).toBeInTheDocument();
  await userEvent.click(screen.getByText('db resource history (1 samples)'));
  expect(screen.getByText('75.0%')).toBeVisible();
  expect(screen.getByText('1.0 MiB / 2.0 MiB')).toBeVisible();
});

it('renders runtime provenance instead of an invented source revision', async () => {
  render(<Investigation summary={null} evidence={[item]} events={[]} />);
  await userEvent.click(screen.getByText(/runtime resources/));
  expect(screen.getByText(/host \/ app \/ db/)).toBeVisible();
  expect(screen.getByText(/Observed: 2026-11-07T10:05:10Z/)).toBeVisible();
  expect(screen.getByText(/Buffer warm-up/)).toBeVisible();
  expect(screen.queryByText(/Revision:/)).not.toBeInTheDocument();
});

it('summarizes observed trace spans with service identity and duration', async () => {
  render(<RuntimeEvidence evidence={[{ ...item, kind: 'runtime_traces', data: { spans: [{ service: 'db', name: 'query', trace_id: 'trace-1', duration_ms: 120, error: true }] } }]} collection={[]} />);
  await userEvent.click(screen.getByText('Observed trace spans (1, sampled)'));
  expect(screen.getByText('query')).toBeVisible();
  expect(screen.getByText('120.0 ms')).toBeVisible();
  expect(screen.getByText('Error observed')).toBeVisible();
});
