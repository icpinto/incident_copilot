import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useId, useRef, useState } from 'react';
import { CheckCircle2, ChevronRight, Clock3, FolderGit2, GitBranch, Layers3, LoaderCircle, MapPin, Pause, Radio, Save, Settings2, TriangleAlert } from 'lucide-react';
import { api, json } from '../api';
import { ErrorNotice, Loading, PageHeading, time } from '../components';
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
  return <article className={`repository-card log-source-card${editing ? ' is-configuring' : ''}`} aria-label={`Log source ${identity}`}>
    <div className="repo-top"><div className="connection-type"><div className="connection-icon"><Radio size={21} /></div><span>Docker project</span></div><span className={`connection-status ${!repository ? 'is-warning' : source.enabled ? 'is-success' : 'is-neutral'}`}>{!repository ? <TriangleAlert size={13} /> : source.enabled ? <CheckCircle2 size={13} /> : <Pause size={13} />}{status}</span></div>
    <h2>{source.project}</h2>
    {duplicate && <small className="source-collector mono">Collector: {source.collector_id}</small>}
    <dl className="connection-metadata">
      <div><dt><FolderGit2 size={14} />Repository</dt><dd>{repository?.name || (source.repository_id ? 'Repository unavailable' : 'No repository connected')}</dd></div>
      <div><dt><MapPin size={14} />Environment</dt><dd>{source.environment || 'unknown'}</dd></div>
    </dl>
    <div className="log-source-activity"><span><Layers3 size={15} />{source.services.length} {source.services.length === 1 ? 'service' : 'services'}</span><span><Clock3 size={15} /><span>Last seen <time dateTime={source.last_seen}>{time(source.last_seen)}</time></span></span></div>
    {!!source.versioned_services?.length && <p className="log-source-version-summary"><GitBranch size={14} /><span>Version matching: {source.versioned_services.join(', ')}</span></p>}
    <div className="log-source-actions"><button ref={configureButton} className="button" aria-label={`Configure ${identity}`} aria-expanded={editing} aria-controls={formId} disabled={editing} onClick={configure}><Settings2 size={15} />Configure</button>{editing && <span className="connection-edit-label">Editing settings</span>}</div>
    {editing && <form id={formId} className="log-source-form" aria-label={`Configure ${identity}`} aria-busy={mutation.isPending} onSubmit={event => { event.preventDefault(); if (!mutation.isPending) mutation.mutate(); }}>
      <div className="source-form-heading"><Settings2 size={16} /><h3>Source configuration</h3></div>
      <fieldset disabled={mutation.isPending}>
        <div className="connection-fields">
        <label>Repository<select autoFocus aria-label={`Repository for ${identity}`} value={repositoryId} onChange={event => setRepositoryId(event.target.value)}>
          <option value="">Choose a repository</option>
          {repositoryId && !repositories.some(repo => repo.id === repositoryId) && <option value={repositoryId} disabled>Repository unavailable — choose another</option>}
          {repositories.map(repo => <option key={repo.id} value={repo.id}>{repo.name}</option>)}
        </select></label>
        <label>Environment<input aria-label={`Environment for ${identity}`} maxLength={120} value={environment} onChange={event => setEnvironment(event.target.value)} /></label>
        </div>
        <div className="source-collection-control"><label className="issue-checkbox"><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} /> Collect errors</label><p>Investigate errors from this Docker project automatically.</p></div>
        {enabled && !repositories.some(repo => repo.id === repositoryId) && <p className="source-setup-hint"><TriangleAlert size={14} />Choose a repository before enabling collection.</p>}
        <details className="log-source-version-settings"><summary><ChevronRight className="disclosure-chevron" size={15} />Version matching</summary>
          <fieldset className="version-services"><legend>Use deployed Git commits</legend>
            <p>Analyze the commit running in each selected service. Requires Git revision labels on images.</p>
            {[...new Set([...source.services, ...versionedServices])].map(name => <label className="issue-checkbox" key={name}><input type="checkbox" checked={versionedServices.includes(name)} onChange={event => setVersionedServices(current => event.target.checked ? [...current, name] : current.filter(item => item !== name))} />{name}{!source.services.includes(name) && <span className="muted"> (not running)</span>}</label>)}
            {!source.services.length && !versionedServices.length && <p>No services discovered yet.</p>}
          </fieldset>
        </details>
        <ErrorNotice error={mutation.error} />
        <div className="button-group source-form-actions"><button className="button primary" disabled={mutation.isPending || (enabled && !repositories.some(repo => repo.id === repositoryId))}>{mutation.isPending ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}{mutation.isPending ? 'Saving…' : 'Save changes'}</button>
          <button type="button" className="button" onClick={() => { setEditing(false); mutation.reset(); requestAnimationFrame(() => configureButton.current?.focus()); }}>Cancel</button></div>
      </fieldset>
    </form>}
    <details className="log-source-details"><summary><ChevronRight className="disclosure-chevron" size={15} />Source details</summary>
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
  return <div className="connections-page"><PageHeading eyebrow="DOCKER INGESTION" title="Log sources" description="Connect Docker projects to repositories to investigate errors automatically." />
    <ErrorNotice error={query.error || repositories.error} />
    {message && <div className="notice connection-success" role="status"><CheckCircle2 size={18} /><span>{message}</span></div>}
    {query.isPending || repositories.isPending ? <Loading /> : (query.isError && !query.data) || (repositories.isError && !repositories.data) ? null : !query.data?.length ? <section className="connection-empty"><div className="connection-icon"><Radio size={26} /></div><h2>Waiting for collector</h2><p>No Compose projects have been discovered. Start the standalone collector on this Docker host. It reports running projects within one minute.</p></section> : <>
      {!repositories.isError && !repositories.data?.filter(repo => !repo.archived).length && <div className="repository-info source-onboarding"><FolderGit2 size={18} /><p><Link to="/repositories">Add a repository</Link> before enabling a source.</p></div>}
      <div className="repository-grid">{query.data.map(source => <SourceRow key={JSON.stringify([source.collector_id, source.project])} source={source} repositories={(repositories.data || []).filter(repo => !repo.archived)} duplicate={query.data.filter(item => item.project === source.project).length > 1} saved={saved} />)}</div>
    </>}
  </div>;
}
