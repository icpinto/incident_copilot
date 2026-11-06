import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useState } from 'react';
import { api, json } from '../api';
import { ErrorNotice, Loading, Notice, PageHeading, Panel } from '../components';
import type { Repository } from '../types';

type Source = { project: string; services: string[]; repository_id: string | null; enabled: boolean; last_seen: string };

function SourceRow({ source, repositories, refresh }: { source: Source; repositories: Repository[]; refresh: () => void }) {
  const [repositoryId, setRepositoryId] = useState(source.repository_id || '');
  const [enabled, setEnabled] = useState(source.enabled);
  const mutation = useMutation({
    mutationFn: () => api<Source>(`/log-sources/${encodeURIComponent(source.project)}`,
      json('PUT', { repository_id: repositoryId || null, enabled })),
    onSuccess: refresh,
  });
  return <article className="repository-card">
    <h2>{source.project}</h2>
    <p>Services: {source.services.length ? source.services.join(', ') : 'None running'}</p>
    <p>Last seen: {new Date(source.last_seen).toLocaleString()}</p>
    <label>Repository
      <select aria-label={`Repository for ${source.project}`} value={repositoryId} onChange={event => setRepositoryId(event.target.value)}>
        <option value="">Choose a repository</option>
        {repositories.map(repo => <option key={repo.id} value={repo.id}>{repo.name}</option>)}
      </select>
    </label>
    <label><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} /> Collect errors</label>
    <div className="repo-actions"><button className="button primary" disabled={mutation.isPending || (enabled && !repositoryId)} onClick={() => mutation.mutate()}>{mutation.isPending ? 'Saving…' : 'Save source'}</button></div>
    <ErrorNotice error={mutation.error} />
  </article>;
}

export function LogSources() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ['log-sources'], queryFn: () => api<Source[]>('/log-sources'), refetchInterval: 15000 });
  const repositories = useQuery({ queryKey: ['repositories'], queryFn: () => api<Repository[]>('/repositories') });
  return <><PageHeading eyebrow="DOCKER INGESTION" title="Log sources" description="Connect discovered Docker Compose projects to repositories for automatic error investigations." />
    <ErrorNotice error={query.error || repositories.error} />
    {query.isPending || repositories.isPending ? <Loading /> : !query.data?.length ? <Panel title="Waiting for collector"><p>No Compose projects have been discovered. Start the standalone collector on this Docker host. It reports running projects within one minute.</p></Panel> : <>
      <Notice>Only enabled projects send error context to Incident Copilot. Collection begins after you save a mapping.</Notice>
      {!repositories.data?.length && <p><Link to="/repositories">Add a repository</Link> before enabling a source.</p>}
      <div className="repository-grid">{query.data.map(source => <SourceRow key={`${source.project}:${source.repository_id}:${source.enabled}`} source={source} repositories={repositories.data || []} refresh={() => client.invalidateQueries({ queryKey: ['log-sources'] })} />)}</div>
    </>}
  </>;
}
