import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Box } from 'lucide-react';
import { api } from './api';
import { ErrorNotice, Panel, time } from './components';
import type { DeploymentContext, DeploymentObservation, DeploymentPage, SourceResolution } from './types';

export function DeployedVersion({ context, resolution }: { context?: DeploymentContext | null; resolution?: SourceResolution }) {
  if (!context) return null;
  return <Panel icon={Box} title="Deployed version" subtitle="Labels declare build provenance; runtime file contents and causation are not verified.">
    <dl className="context-list">
      <div><dt>Environment / service</dt><dd>{context.environment || 'unknown'} / {context.service}</dd></div>
      <div><dt>Release version</dt><dd>{context.version || 'unknown'}</dd></div>
      <div><dt>Declared commit</dt><dd className="mono">{context.revision || 'unknown'}</dd></div>
      <div><dt>Analyzed commit</dt><dd className="mono">{resolution?.analyzed_commit || 'unknown'}</dd></div>
      <div><dt>Source resolution</dt><dd>{resolution?.status || 'unknown'}</dd></div>
      <div><dt>Image reference</dt><dd className="mono">{context.image_ref || 'unknown'}</dd></div>
      <div><dt>Image ID</dt><dd className="mono">{context.image_id || 'unknown'}</dd></div>
      <div><dt>Registry digests</dt><dd className="mono">{context.repo_digests?.join(', ') || 'unknown'}</dd></div>
      <div><dt>Collector / container</dt><dd className="mono">{context.collector_id} / {context.container_id}</dd></div>
    </dl>{context.runtime_config && <details className="deep-analysis"><summary>Observed runtime configuration</summary>
      <p>Collector snapshot. Sensitive values are redacted; mount contents are not captured.</p>
      {context.runtime_config.status === 'available' ? <>
        <h4>Environment</h4><dl className="context-list">{context.runtime_config.environment.map(item => <div key={item.name}><dt>{item.name}</dt><dd className="mono">{item.value}</dd></div>)}</dl>
        <h4>Mounts</h4><ul>{context.runtime_config.mounts.map((mount, index) => <li key={`${mount.destination}-${index}`}><span className="mono">{mount.destination}</span> · {mount.type} · {mount.read_only ? 'read only' : 'read/write'}</li>)}</ul>
        {context.runtime_config.truncated && <p>Additional settings were omitted by the collector.</p>}
      </> : <p>Container configuration could not be inspected.</p>}
    </details>}{resolution?.reason && <p role={resolution.status === 'unavailable' ? 'alert' : undefined}>{resolution.reason}</p>}
  </Panel>;
}

export function DeploymentPicker({ repositoryId, value, onChange }: { repositoryId: string; value: string; onChange: (deployment: DeploymentObservation | null) => void }) {
  const query = useInfiniteQuery({ queryKey: ['deployment-options', repositoryId], enabled: !!repositoryId,
    initialPageParam: 1,
    queryFn: ({ pageParam }) => api<DeploymentPage>(`/deployments?repository_id=${encodeURIComponent(repositoryId)}&page=${pageParam}`),
    getNextPageParam: page => page.page * page.page_size < page.total ? page.page + 1 : undefined });
  const items = query.data?.pages.flatMap(page => page.items || []) || [];
  return <div className="deployment-picker"><label>Observed deployment <span className="optional">optional</span>
    <select aria-label="Observed deployment" disabled={!repositoryId || query.isPending} value={value}
      onChange={event => onChange(items.find(item => item.deployment_id === event.target.value) || null)}>
      <option value="">Use the configured repository source</option>
      {items.map(item => <option key={item.deployment_id} value={item.deployment_id}>{item.environment} / {item.service} / {item.version || item.revision?.slice(0, 12) || 'unknown commit'} / {item.container_id.slice(0, 12)}</option>)}
    </select></label>
    <p>Select the container that produced these logs. Its declared commit will be used even if the repository has advanced.</p>
    {query.hasNextPage && <button type="button" className="button" disabled={query.isFetchingNextPage} onClick={() => query.fetchNextPage()}>Load more deployments</button>}
    <ErrorNotice error={query.error} />
  </div>;
}

export function ObservedVersions({ collectorId, project }: { collectorId: string; project: string }) {
  const [page, setPage] = useState(1);
  const query = useQuery({ queryKey: ['deployments', collectorId, project, page],
    queryFn: () => api<DeploymentPage>(`/deployments?collector_id=${encodeURIComponent(collectorId)}&project=${encodeURIComponent(project)}&page=${page}`), refetchInterval: 15000 });
  return <details><summary>Observed container versions</summary><ErrorNotice error={query.error} />
    {query.data?.items?.length ? <ul>{query.data.items.map(item => <li key={item.deployment_id}>
      <strong>{item.service} / {item.version || 'unknown version'}</strong>
      <p className="mono">Commit: {item.revision || 'unknown'}<br />Container: {item.container_id.slice(0, 12)}</p>
      <small>First observed: {time(item.first_observed)}<br />Last observed: {time(item.last_observed)}</small>
    </li>)}</ul> : <p>No container versions observed yet.</p>}
    <div className="button-group"><button className="button" disabled={page === 1} onClick={() => setPage(value => value - 1)}>Previous versions</button>
      <button className="button" disabled={!query.data || page * query.data.page_size >= query.data.total} onClick={() => setPage(value => value + 1)}>Next versions</button></div>
  </details>;
}
