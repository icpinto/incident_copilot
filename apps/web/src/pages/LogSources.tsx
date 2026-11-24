import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useId, useRef, useState } from 'react';
import { api, json } from '../api';
import { ErrorNotice, Loading, Notice, PageHeading, Panel, time } from '../components';
import type { Repository } from '../types';
import { ObservedVersions } from '../Deployment';

type Source = { collector_id: string; environment: string; versioned_services: string[]; project: string; services: string[]; repository_id: string | null; enabled: boolean; last_seen: string };

function SourceRow({ source, repositories, duplicate, saved }: { source: Source; repositories: Repository[]; duplicate: boolean; saved: (source: Source) => void }) {
  const [editing, setEditing] = useState(false);
  const [repositoryId, setRepositoryId] = useState('');
  const [enabled, setEnabled] = useState(false);
  const [environment, setEnvironment] = useState('');
  const [versionedServices, setVersionedServices] = useState<string[]>([]);
  const formId = useId();
  const configureButton = useRef<HTMLButtonElement>(null);
  const repository = repositories.find(repo => repo.id === source.repository_id);
  const identity = `${source.project} (${source.collector_id})`;
  const status = !repository ? 'Setup required' : source.enabled ? 'Enabled' : 'Disabled';
  const mutation = useMutation({
    mutationFn: () => api<Source>(`/log-sources/${encodeURIComponent(source.project)}?collector_id=${encodeURIComponent(source.collector_id)}`,
      json('PUT', { repository_id: repositoryId || null, enabled, environment: environment.trim() || 'unknown', versioned_services: versionedServices })),
    onSuccess: updated => { saved(updated); setEditing(false); requestAnimationFrame(() => configureButton.current?.focus()); },
  });
  const configure = () => {
    setRepositoryId(source.repository_id || ''); setEnabled(source.enabled);
    setEnvironment(source.environment || 'unknown'); setVersionedServices(source.versioned_services || []);
    mutation.reset(); setEditing(true);
  };
  return <article className="repository-card log-source-card" aria-label={`Log source ${identity}`}>
    <div className="log-source-heading"><h2>{source.project}</h2><span className={`badge source-${status.toLowerCase().replaceAll(' ', '-')}`}>{status}</span></div>
    {duplicate && <small className="mono">Collector: {source.collector_id}</small>}
    <p>{repository?.name || (source.repository_id ? 'Repository unavailable' : 'No repository connected')} · {source.environment || 'unknown'}</p>
    <p className="muted">{source.services.length} {source.services.length === 1 ? 'service' : 'services'} · Last seen <time dateTime={source.last_seen}>{time(source.last_seen)}</time></p>
    {!!source.versioned_services?.length && <p className="log-source-version-summary">Version matching: {source.versioned_services.join(', ')}</p>}
    <div className="log-source-actions"><button ref={configureButton} className="button" aria-label={`Configure ${identity}`} aria-expanded={editing} aria-controls={formId} disabled={editing} onClick={configure}>Configure</button></div>
    {editing && <form id={formId} className="log-source-form" aria-label={`Configure ${identity}`} onSubmit={event => { event.preventDefault(); if (!mutation.isPending) mutation.mutate(); }}>
      <fieldset disabled={mutation.isPending}>
        <label>Repository<select autoFocus aria-label={`Repository for ${identity}`} value={repositoryId} onChange={event => setRepositoryId(event.target.value)}>
          <option value="">Choose a repository</option>
          {repositoryId && !repositories.some(repo => repo.id === repositoryId) && <option value={repositoryId} disabled>Repository unavailable — choose another</option>}
          {repositories.map(repo => <option key={repo.id} value={repo.id}>{repo.name}</option>)}
        </select></label>
        <label>Environment<input aria-label={`Environment for ${identity}`} maxLength={120} value={environment} onChange={event => setEnvironment(event.target.value)} /></label>
        <label className="issue-checkbox"><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} /> Collect errors</label>
        {enabled && !repositories.some(repo => repo.id === repositoryId) && <p>Choose a repository before enabling collection.</p>}
        <details className="log-source-version-settings"><summary>Version matching</summary>
          <fieldset className="version-services"><legend>Use deployed Git commits</legend>
            <p>Analyze the commit running in each selected service. Requires Git revision labels on images.</p>
            {[...new Set([...source.services, ...versionedServices])].map(name => <label className="issue-checkbox" key={name}><input type="checkbox" checked={versionedServices.includes(name)} onChange={event => setVersionedServices(current => event.target.checked ? [...current, name] : current.filter(item => item !== name))} />{name}{!source.services.includes(name) && <span className="muted"> (not running)</span>}</label>)}
            {!source.services.length && !versionedServices.length && <p>No services discovered yet.</p>}
          </fieldset>
        </details>
        <ErrorNotice error={mutation.error} />
        <div className="button-group"><button className="button primary" disabled={mutation.isPending || (enabled && !repositories.some(repo => repo.id === repositoryId))}>{mutation.isPending ? 'Saving…' : 'Save changes'}</button>
          <button type="button" className="button" onClick={() => { setEditing(false); mutation.reset(); requestAnimationFrame(() => configureButton.current?.focus()); }}>Cancel</button></div>
      </fieldset>
    </form>}
    <details className="log-source-details"><summary>Source details</summary>
      <p>Collector: <span className="mono">{source.collector_id}</span></p>
      <p>Services: {source.services.join(', ') || 'None running'}</p>
      <ObservedVersions collectorId={source.collector_id} project={source.project} />
    </details>
  </article>;
}

export function LogSources() {
  const client = useQueryClient();
  const [message, setMessage] = useState('');
  const query = useQuery({ queryKey: ['log-sources'], queryFn: () => api<Source[]>('/log-sources'), refetchInterval: 15000 });
  const repositories = useQuery({ queryKey: ['repositories'], queryFn: () => api<Repository[]>('/repositories') });
  const saved = (source: Source) => {
    client.setQueryData<Source[]>(['log-sources'], current => current?.map(item => item.collector_id === source.collector_id && item.project === source.project ? source : item));
    setMessage(`Settings saved for ${source.project} (${source.collector_id}).`);
    void client.invalidateQueries({ queryKey: ['log-sources'] });
  };
  return <><PageHeading eyebrow="DOCKER INGESTION" title="Log sources" description="Connect Docker projects to repositories to investigate errors automatically." />
    <ErrorNotice error={query.error || repositories.error} />
    {message && <Notice>{message}</Notice>}
    {query.isPending || repositories.isPending ? <Loading /> : !query.data?.length ? <Panel title="Waiting for collector"><p>No Compose projects have been discovered. Start the standalone collector on this Docker host. It reports running projects within one minute.</p></Panel> : <>
      {!repositories.data?.filter(repo => !repo.archived).length && <p><Link to="/repositories">Add a repository</Link> before enabling a source.</p>}
      <div className="repository-grid">{query.data.map(source => <SourceRow key={JSON.stringify([source.collector_id, source.project])} source={source} repositories={(repositories.data || []).filter(repo => !repo.archived)} duplicate={query.data.filter(item => item.project === source.project).length > 1} saved={saved} />)}</div>
    </>}
  </>;
}
