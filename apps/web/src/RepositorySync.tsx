import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { RotateCw } from 'lucide-react';
import { useEffect } from 'react';
import { api, json } from './api';
import { ErrorNotice } from './components';
import type { Repository, RepositorySync as Sync } from './types';

const stages: Record<string, string> = {
  queued: 'Waiting for worker', checking: 'Checking GitHub', fetching: 'Downloading changes',
  indexing: 'Indexing source', preparing_graph: 'Preparing code graph', retrying: 'Retrying connection',
};

export function RepositorySync({ repo }: { repo: Repository }) {
  const client = useQueryClient();
  const key = ['repository-sync', repo.id];
  const query = useQuery({ queryKey: key, queryFn: () => api<Sync>(`/repositories/${repo.id}/sync`),
    refetchInterval: q => ['queued', 'running'].includes(q.state.data?.status || '') ? 2000 : false });
  const sync = useMutation({ mutationFn: () => api<Sync>(`/repositories/${repo.id}/sync`, json('POST')),
    onSuccess: data => client.setQueryData(key, data) });
  const state = query.data;
  const busy = sync.isPending || state?.status === 'queued' || state?.status === 'running';
  useEffect(() => {
    if (state?.status === 'completed' || state?.status === 'failed') {
      client.invalidateQueries({ queryKey: ['repositories'] });
      client.invalidateQueries({ queryKey: ['repository-graph', repo.id] });
    }
  }, [state?.status, state?.completed_at, client, repo.id]);
  return <section className="repository-graph" aria-label={`GitHub sync for ${repo.name}`}>
    <div className="inline-heading"><strong>GitHub sync</strong><span role="status">{busy ? stages[state?.stage || 'queued'] || 'Syncing…' : state?.status === 'failed' ? 'Needs attention' : state?.result === 'unchanged' ? 'No changes at last check' : repo.synced_revision ? 'Synced' : 'Not synced'}</span></div>
    <ErrorNotice error={query.error || sync.error || state?.error} />
    {state?.connection_error && <p>{state.connection_error}</p>}
    {repo.synced_revision ? <p className="mono" title={repo.synced_revision}>Synced commit: {repo.synced_revision.slice(0, 12)}</p> : <p>Check &amp; sync before starting an analysis.</p>}
    {repo.last_checked_at && <p>Last checked: {new Date(repo.last_checked_at).toLocaleString()}</p>}
    {repo.last_synced_at && <p>Last synced: {new Date(repo.last_synced_at).toLocaleString()}</p>}
    {state?.graph_preparation?.status === 'unavailable' && <p>Code graph preparation is unavailable. Source diagnosis is ready; graph preparation can be retried below.</p>}
    <button className="button" disabled={busy || query.isPending} onClick={() => sync.mutate()}><RotateCw size={14} />{busy ? 'Syncing…' : 'Check & sync'}</button>
    <p>GitHub is checked only when you request a sync. Analyses use the last successfully synced commit.</p>
  </section>;
}
