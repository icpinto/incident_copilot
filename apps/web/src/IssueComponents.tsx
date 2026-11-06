import { useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { api } from './api';
import { Badge, Empty, ErrorNotice, time } from './components';
import type { Issue, Repository } from './types';

export const issueStatuses = ['open', 'acknowledged', 'investigating', 'resolved', 'ignored'] as const;
export const dimensionFilter = { repository: 'repository_id', service: 'service_name', error_class: 'error_class', file: 'file_path', function: 'function_name' };
export const changeLabel = (issue: { change: number; change_percent: number | null }) => issue.change_percent == null
  ? 'No previous occurrences' : `${issue.change > 0 ? '+' : ''}${issue.change} (${issue.change_percent > 0 ? '+' : ''}${issue.change_percent}%)`;

export function useErrorParams() {
  const [params, setParams] = useSearchParams();
  const update = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    value ? next.set(key, value) : next.delete(key);
    if (key !== 'page') next.delete('page');
    setParams(next, { replace: true });
  };
  return { params, setParams, update };
}

export function IssueFilters({ showSort = false }: { showSort?: boolean }) {
  const { params, setParams, update } = useErrorParams();
  const repos = useQuery({ queryKey: ['repositories'], queryFn: () => api<Repository[]>('/repositories') });
  const fields = [['service_name', 'Service'], ['error_class', 'Exception class'], ['file_path', 'Stack file'], ['function_name', 'Function'], ['owner', 'Owner'], ['incident_id', 'Investigation ID']];
  return <><ErrorNotice error={repos.error} /><div className="issue-filters">
    <div className="issue-filter-primary">
      <label>Search<input aria-label="Search issues" placeholder="Title or issue ID" value={params.get('q') || ''} onChange={e => update('q', e.target.value)} /></label>
      <label>Repository<select aria-label="Issue repository" value={params.get('repository_id') || ''} onChange={e => update('repository_id', e.target.value)}><option value="">All repositories</option>{repos.data?.map(repo => <option key={repo.id} value={repo.id}>{repo.name}</option>)}</select></label>
      <label>Status<select aria-label="Issue status filter" value={params.get('status') || ''} onChange={e => update('status', e.target.value)}><option value="">All statuses</option>{issueStatuses.map(status => <option key={status}>{status}</option>)}</select></label>
      <label>Window<select aria-label="Error time window" value={params.get('window') || (params.has('start_at') || params.has('end_at') ? 'custom' : '14')} onChange={e => {
        const next = new URLSearchParams(params); const days = Number(e.target.value);
        const end = new Date(); next.set('end_at', end.toISOString()); next.set('start_at', new Date(end.getTime() - days * 86400000).toISOString()); next.set('window', String(days)); next.delete('page'); setParams(next, { replace: true });
      }}><option value="14">Last 14 days</option><option value="1">Last 24 hours</option><option value="7">Last 7 days</option><option value="30">Last 30 days</option><option value="custom" disabled>Custom window</option></select></label>
      {showSort && <label>Sort<select aria-label="Issue sort" value={params.get('sort') || 'occurrences'} onChange={e => update('sort', e.target.value)}><option value="occurrences">Most frequent</option><option value="growth">Fastest growing</option><option value="last_seen">Last seen</option></select></label>}
      <button className="button" onClick={() => setParams({})}>Reset filters</button>
    </div>
    <details open={fields.some(([key]) => params.has(key)) || params.get('window') === 'custom' || undefined}><summary>More filters</summary><div className="issue-filter-secondary">
      {fields.map(([key, label]) => <label key={key}>{label}<input aria-label={`Issue ${label.toLowerCase()}`} value={params.get(key) || ''} onChange={e => update(key, e.target.value)} /></label>)}
      {(['start_at', 'end_at'] as const).map(key => <label key={key}>{key === 'start_at' ? 'From' : 'Until'} (UTC)<input aria-label={key === 'start_at' ? 'Errors from UTC' : 'Errors until UTC'} type="datetime-local" value={(params.get(key) || '').replace(/Z|\+00:00$/, '').slice(0, 16)} onChange={e => {
        const next = new URLSearchParams(params); e.target.value ? next.set(key, `${e.target.value}:00Z`) : next.delete(key); next.set('window', 'custom'); next.delete('page'); setParams(next, { replace: true });
      }} /></label>)}
    </div></details>
  </div></>;
}

export function IssueTable({ items }: { items: Issue[] }) {
  return items.length ? <div className="table-scroll" role="region" aria-label="Issue table" tabIndex={0}><table><thead><tr><th scope="col">Issue</th><th scope="col">Status / owner</th><th scope="col">Occurrences</th><th scope="col">Change</th><th scope="col">First / last seen</th></tr></thead><tbody>{items.map(issue => <tr key={issue.id}>
    <td><Link className="incident-title" to={`/issues/${issue.id}`}>{issue.title}</Link><small>{issue.repository_name} · {issue.id.slice(0, 8)}</small></td>
    <td><Badge status={issue.status} /><small>{issue.owner || 'Unassigned'}</small></td>
    <td><strong>{issue.occurrence_count.toLocaleString()}</strong><small>{issue.total_count.toLocaleString()} total</small></td>
    <td className={issue.change > 0 ? 'issue-increase' : 'muted'}>{changeLabel(issue)}</td>
    <td><span>{issue.first_seen ? time(issue.first_seen) : 'Unknown'}</span><small>{issue.last_seen ? time(issue.last_seen) : 'Unknown'}</small></td>
  </tr>)}</tbody></table></div> : <Empty title="No matching issues" description="New uploads populate error tracking. Try adjusting the filters." action={false} />;
}

export function IssuePagination({ page, total, pageSize, onPage }: { page: number; total: number; pageSize: number; onPage: (page: number) => void }) {
  return <div className="pagination"><span>Page {page} · {total} results</span><div className="button-group"><button className="button" disabled={page <= 1} onClick={() => onPage(page - 1)}>Previous</button><button className="button" disabled={page * pageSize >= total} onClick={() => onPage(page + 1)}>Next</button></div></div>;
}
