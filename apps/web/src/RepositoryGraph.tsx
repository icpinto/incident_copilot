import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { RotateCw } from 'lucide-react';
import { api, json } from './api';
import { ErrorNotice } from './components';
import type { GraphPreparation, Repository } from './types';

const labels: Record<GraphPreparation['status'], string> = {
  disabled: 'Disabled', external: 'External service', not_prepared: 'Not prepared', queued: 'Queued',
  preparing: 'Preparing', ready: 'Ready', stale: 'Refresh needed', unavailable: 'Unavailable',
};
const messages: Record<string, string> = {
  checkout_not_clean_or_unavailable: 'Use a clean, committed Git checkout for graph evidence.',
  repository_outside_mount: 'This repository must be inside the graph service’s source mount.',
  repository_unavailable: 'The graph service cannot access this repository.',
  source_changed: 'Source changed during preparation. Retry after committing your changes.',
  source_snapshot_mismatch: 'Source changed. A new analysis will prepare the current revision.',
  preparation_timeout: 'Preparation reached its deadline. Diagnosis can continue without graph evidence.',
  preparation_failed: 'Preparation failed. Check the graph service and retry.',
  manager_unavailable: 'The graph service is unreachable. Diagnosis remains available.',
};

export function RepositoryGraph({ repo }: { repo: Repository }) {
  const client = useQueryClient();
  const key = ['repository-graph', repo.id, repo.repo_root];
  const query = useQuery({ queryKey: key, queryFn: () => api<GraphPreparation>(`/repositories/${repo.id}/graph`),
    refetchInterval: q => ['queued', 'preparing'].includes(q.state.data?.status || '') ? 2000 : 10000 });
  const refresh = useMutation({ mutationFn: () => api<GraphPreparation>(`/repositories/${repo.id}/graph/refresh`, json('POST')),
    onSuccess: data => client.setQueryData(key, data) });
  const state = query.data;
  const busy = refresh.isPending || state?.status === 'queued' || state?.status === 'preparing';
  return <section className="repository-graph" aria-label={`Code graph for ${repo.name}`}>
    <div className="inline-heading"><strong>Code graph</strong><span role="status">{state ? labels[state.status] : query.isPending ? 'Checking…' : 'Unavailable'}</span></div>
    <ErrorNotice error={query.error || refresh.error} />
    {state?.reason && <p>{messages[state.reason] || 'Graph evidence is unavailable. Diagnosis remains available.'}</p>}
    {state?.indexed_revision && <p className="mono" title={state.indexed_revision}>Indexed commit: {state.indexed_revision.slice(0, 12)}{state.source_revision && state.source_revision !== state.indexed_revision ? ` · Current: ${state.source_revision.slice(0, 12)}` : ''}</p>}
    {state?.indexed_at && <small>Indexed {new Date(state.indexed_at).toLocaleString()}</small>}
    {state && !['disabled', 'external'].includes(state.status) && <button className="button" disabled={busy} onClick={() => refresh.mutate()}><RotateCw size={14} />{busy ? 'Preparing graph…' : state.indexed_revision ? 'Refresh graph' : 'Prepare graph'}</button>}
  </section>;
}
