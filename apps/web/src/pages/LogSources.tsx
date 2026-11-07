import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useState } from 'react';
import { api, json } from '../api';
import { ErrorNotice, Loading, Notice, PageHeading, Panel } from '../components';
import type { Repository } from '../types';
import { ObservedVersions } from '../Deployment';

type Source = { collector_id: string; environment: string; versioned_services: string[]; project: string; services: string[]; repository_id: string | null; enabled: boolean; last_seen: string; runtime_enabled?: boolean; runtime_dependencies?: Record<string, string[]>; telemetry_profile?: string | null; runtime_collector?: { updated_at: string; capabilities: string[]; helper?: { status?: string; sampled_containers?: number; sampling_omissions?: number } } | null };

function SourceRow({ source, repositories, refresh }: { source: Source; repositories: Repository[]; refresh: () => void }) {
  const [repositoryId, setRepositoryId] = useState(source.repository_id || '');
  const [enabled, setEnabled] = useState(source.enabled);
  const [environment, setEnvironment] = useState(source.environment || 'unknown');
  const [versionedServices, setVersionedServices] = useState(source.versioned_services || []);
  const [runtimeEnabled, setRuntimeEnabled] = useState(source.runtime_enabled ?? true);
  const [dependencies, setDependencies] = useState(source.runtime_dependencies || {});
  const [profile, setProfile] = useState(source.telemetry_profile || '');
  const mutation = useMutation({
    mutationFn: () => api<Source>(`/log-sources/${encodeURIComponent(source.project)}?collector_id=${encodeURIComponent(source.collector_id)}`,
      json('PUT', { repository_id: repositoryId || null, enabled, environment: environment || 'unknown', versioned_services: versionedServices, runtime_enabled: runtimeEnabled, runtime_dependencies: dependencies, telemetry_profile: profile || null })),
    onSuccess: refresh,
  });
  return <article className="repository-card">
    <h2>{source.project}</h2><small>Collector: {source.collector_id}</small>
    <p>Services: {source.services.length ? source.services.join(', ') : 'None running'}</p>
    <p>Last seen: {new Date(source.last_seen).toLocaleString()}</p>
    <label>Repository
      <select aria-label={`Repository for ${source.project}`} value={repositoryId} onChange={event => setRepositoryId(event.target.value)}>
        <option value="">Choose a repository</option>
        {repositories.map(repo => <option key={repo.id} value={repo.id}>{repo.name}</option>)}
      </select>
    </label>
    <label>Environment<input aria-label={`Environment for ${source.project}`} maxLength={120} value={environment} onChange={event => setEnvironment(event.target.value)} /></label>
    <fieldset className="version-services"><legend>Analyze deployed versions</legend><p>Selected services require a full Git commit label and matching source. Errors are saved if source is unavailable.</p>
      {[...new Set([...source.services, ...versionedServices])].map(name => <label className="issue-checkbox" key={name}><input type="checkbox" checked={versionedServices.includes(name)} onChange={event => setVersionedServices(current => event.target.checked ? [...current, name] : current.filter(item => item !== name))} />{name}</label>)}
    </fieldset>
    <label><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} /> Collect errors</label>
    <fieldset className="version-services"><legend>Runtime evidence</legend>
      <label><input type="checkbox" checked={runtimeEnabled} onChange={event => setRuntimeEnabled(event.target.checked)} /> Retrieve passive runtime evidence</label>
      <p>{source.runtime_collector ? `Capabilities: ${source.runtime_collector.capabilities.join(', ')}` : 'Waiting for the runtime helper to register.'}</p>
      {source.runtime_collector && <p>Helper last seen: {new Date(source.runtime_collector.updated_at).toLocaleString()} · {Date.now() - new Date(source.runtime_collector.updated_at).getTime() > 60000 ? 'Stale' : source.runtime_collector.helper?.status || 'Waiting for helper'} · Sampled containers: {source.runtime_collector.helper?.sampled_containers ?? 0} · Sampling omissions: {source.runtime_collector.helper?.sampling_omissions ?? 0}</p>}
      <label>Telemetry profile<input aria-label={`Telemetry profile for ${source.project}`} value={profile} maxLength={120} onChange={event => setProfile(event.target.value)} /></label>
      <p>Choose up to four dependencies per service. Traces can link additional mapped services within the query limit.</p>
      {source.services.map(name => <label key={name}>Dependencies of {name}<select multiple aria-label={`Dependencies of ${name} in ${source.project}`} value={dependencies[name] || []} onChange={event => setDependencies(current => ({ ...current, [name]: Array.from(event.target.selectedOptions, option => option.value).slice(0, 4) }))}>{source.services.filter(item => item !== name).map(item => <option key={item} value={item}>{item}</option>)}</select></label>)}
    </fieldset>
    <div className="repo-actions"><button className="button primary" disabled={mutation.isPending || (enabled && !repositoryId)} onClick={() => mutation.mutate()}>{mutation.isPending ? 'Saving…' : 'Save source'}</button></div>
    <ErrorNotice error={mutation.error} />
    <ObservedVersions collectorId={source.collector_id} project={source.project} />
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
      <div className="repository-grid">{query.data.map(source => <SourceRow key={`${source.collector_id}:${source.project}:${source.repository_id}:${source.enabled}:${source.environment}:${source.versioned_services?.join(',')}`} source={source} repositories={repositories.data || []} refresh={() => client.invalidateQueries({ queryKey: ['log-sources'] })} />)}</div>
    </>}
  </>;
}
