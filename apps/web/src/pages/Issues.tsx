import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useEffect, useState } from 'react';
import { api, json } from '../api';
import { Badge, Empty, ErrorNotice, Loading, Notice, PageHeading, Panel, time } from '../components';
import { IssueFilters, IssuePagination, issueStatuses, IssueTable, useErrorParams } from '../IssueComponents';
import type { ActivityPage, ErrorOccurrence, Issue, IssueActivity, IssuePage, IssueStatus, OccurrencePage } from '../types';

export function Issues() {
  const { params, update } = useErrorParams();
  const query = useQuery({ queryKey: ['issues', params.toString()], queryFn: () => api<IssuePage>(`/issues?${params}`), refetchInterval: 15000 });
  return <><PageHeading eyebrow="PERSISTENT ERROR TRACKING" title="Issues" description="Group recurring errors and track ownership through resolution." />
    <IssueFilters showSort /><ErrorNotice error={query.error} />{query.isPending ? <Loading /> : query.data && <><Notice>Uploaded data only · Tracking new submissions since {time(query.data.tracking_since)}.</Notice><Panel title="Recurring issues" subtitle={`Counts from ${time(query.data.start_at)} to ${time(query.data.end_at)}`}>
      <IssueTable items={query.data.items} /><IssuePagination page={query.data.page} total={query.data.total} pageSize={query.data.page_size} onPage={page => update('page', String(page))} />
    </Panel></>}
  </>;
}

function Triage({ issue, onSaved }: { issue: Issue; onSaved: (issue: Issue) => void }) {
  const [title, setTitle] = useState(issue.title);
  const [owner, setOwner] = useState(issue.owner);
  const [status, setStatus] = useState<IssueStatus>(issue.status);
  const [note, setNote] = useState(issue.resolution_note);
  useEffect(() => { setTitle(issue.title); setOwner(issue.owner); setStatus(issue.status); setNote(issue.resolution_note); }, [issue.title, issue.owner, issue.status, issue.resolution_note]);
  const save = useMutation({ mutationFn: () => api<Issue>(`/issues/${issue.id}`, json('PATCH', { title, owner, status, resolution_note: note })), onSuccess: onSaved });
  return <Panel title="Ownership and triage" subtitle="Investigation completion does not resolve this issue."><form className="issue-triage" onSubmit={e => { e.preventDefault(); save.mutate(); }}>
    <label>Title<input aria-label="Issue title" required maxLength={200} value={title} onChange={e => setTitle(e.target.value)} /></label>
    <div className="issue-form-row"><label>Owner<input aria-label="Issue owner" placeholder="Person or team" maxLength={120} value={owner} onChange={e => setOwner(e.target.value)} /></label><label>Status<select aria-label="Issue status" value={status} onChange={e => setStatus(e.target.value as IssueStatus)}>{issueStatuses.map(value => <option key={value}>{value}</option>)}</select></label></div>
    <label>Resolution note<textarea aria-label="Resolution note" maxLength={4000} rows={3} value={note} onChange={e => setNote(e.target.value)} /></label>
    <ErrorNotice error={save.error} /><div><button className="button primary" disabled={save.isPending || !title.trim()}>{save.isPending ? 'Saving…' : 'Save issue'}</button>{save.isSuccess && <span className="subtle issue-saved" role="status">Changes saved</span>}</div>
  </form></Panel>;
}

function MergeIssues({ issue, onSaved }: { issue: Issue; onSaved: (issue: Issue) => void }) {
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<string[]>([]);
  const [page, setPage] = useState(1);
  const query = useQuery({ queryKey: ['issues', 'merge', issue.repository_id, search, page], queryFn: () => api<IssuePage>(`/issues?repository_id=${encodeURIComponent(issue.repository_id)}&q=${encodeURIComponent(search)}&page=${page}`) });
  const merge = useMutation({ mutationFn: () => api<Issue>(`/issues/${issue.id}/merge`, json('POST', { source_issue_ids: selected })), onSuccess: onSaved });
  return <Panel title="Merge into this issue" subtitle="The current issue keeps its owner and status. Future matching errors follow this issue.">
    <div className="issue-triage"><label>Find issues in {issue.repository_name}<input aria-label="Find merge sources" placeholder="Title or issue ID" value={search} onChange={e => { setSearch(e.target.value); setPage(1); }} /></label>
      <ErrorNotice error={query.error || merge.error} />{query.isPending ? <Loading /> : <div className="merge-candidates">{query.data?.items.filter(item => item.id !== issue.id).map(item => <label className="issue-checkbox" key={item.id}><input type="checkbox" checked={selected.includes(item.id)} onChange={e => setSelected(previous => e.target.checked ? [...previous, item.id] : previous.filter(id => id !== item.id))} />{item.title}<small>{item.status} · {item.total_count} occurrences</small></label>)}</div>}
      {query.data && <IssuePagination page={page} total={query.data.total} pageSize={query.data.page_size} onPage={setPage} />}
      <button className="button" disabled={merge.isPending || !selected.length || selected.length > 100} onClick={() => merge.mutate()}>{merge.isPending ? 'Merging…' : `Merge ${selected.length} selected issues`}</button>
    </div>
  </Panel>;
}

function Occurrence({ item, selected, onSelect }: { item: ErrorOccurrence; selected: boolean; onSelect: (selected: boolean) => void }) {
  return <article className="error-occurrence"><div className="occurrence-heading"><label className="issue-checkbox"><input aria-label={`Select occurrence ${item.id}`} type="checkbox" checked={selected} onChange={e => onSelect(e.target.checked)} /><strong>{item.event.service || 'Unknown service'}</strong></label><span>{time(item.effective_at)} · {item.timestamp_source === 'event' ? 'Event timestamp' : 'Submission time fallback'}</span></div>
    <details><summary>{item.event.exceptions.map(exception => exception.type).join(' → ') || item.event.message.split('\n')[0].slice(0, 180)}</summary><pre className="code-block">{item.event.message}</pre>
      {(item.event.trace_id || item.event.request_id) && <p className="mono">{item.event.trace_id && `Trace: ${item.event.trace_id} `}{item.event.request_id && `Request: ${item.event.request_id}`}</p>}
      {!!item.event.stack_frames.length && <ul>{item.event.stack_frames.map((frame, index) => <li key={index}><code>{frame.file_path || 'Unknown file'}{frame.line_number ? `:${frame.line_number}` : ''} {frame.function_name}</code></li>)}</ul>}
      {!!item.event.limitations.length && <ul className="subtle">{item.event.limitations.map((limitation, index) => <li key={index}>{limitation}</li>)}</ul>}
    </details>
    <div className="occurrence-links">{item.investigations.map(run => <Link className="text-link" key={run.id} to={`/incidents/${run.id}?view=logs&line_start=${item.event.line_start}&line_end=${item.event.line_end}`}>{run.title} · original lines {item.event.line_start}–{item.event.line_end}</Link>)}</div>
  </article>;
}

function ActivityDetails({ item }: { item: IssueActivity }) {
  const details = item.details;
  if (item.kind === 'updated') {
    const changes = details.changes as Record<string, { from: string | null; to: string | null }>;
    return <ul>{Object.entries(changes).filter(([key]) => key !== 'resolved_at').map(([key, value]) => <li key={key}>{key.replaceAll('_', ' ')}: {value.from || 'empty'} → {value.to || 'empty'}</li>)}</ul>;
  }
  return <>{typeof details.reason === 'string' && <p>{details.reason}</p>}
    {Array.isArray(details.source_issue_ids) && <p>Merged {details.source_issue_ids.length} issues into this issue.</p>}
    {typeof details.target_issue_id === 'string' && <Link className="text-link" to={`/issues/${details.target_issue_id}`}>View destination issue</Link>}
    {typeof details.source_issue_id === 'string' && <Link className="text-link" to={`/issues/${details.source_issue_id}`}>View original issue</Link>}
    {Array.isArray(details.occurrence_ids) && <p>{details.occurrence_ids.length} occurrences moved.</p>}
  </>;
}

export function IssueDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [page, setPage] = useState(1);
  const [activityPage, setActivityPage] = useState(1);
  const [selected, setSelected] = useState<string[]>([]);
  const [splitTitle, setSplitTitle] = useState('');
  const [mergeOpen, setMergeOpen] = useState(false);
  const query = useQuery({ queryKey: ['issue', id], queryFn: () => api<Issue>(`/issues/${id}`), refetchInterval: 15000 });
  const occurrences = useQuery({ queryKey: ['issue-occurrences', id, page], queryFn: () => api<OccurrencePage>(`/issues/${id}/occurrences?page=${page}`), refetchInterval: 15000 });
  const activity = useQuery({ queryKey: ['issue-activity', id, activityPage], queryFn: () => api<ActivityPage>(`/issues/${id}/activity?page=${activityPage}`), refetchInterval: 15000 });
  const saved = (issue: Issue) => {
    client.setQueryData(['issue', issue.id], issue);
    for (const key of ['issues', 'issue', 'issue-occurrences', 'issue-activity', 'error-intelligence']) client.invalidateQueries({ queryKey: [key] });
    setSelected([]); setMergeOpen(false);
  };
  const split = useMutation({ mutationFn: () => api<Issue>(`/issues/${id}/split`, json('POST', { occurrence_ids: selected, ...(splitTitle.trim() ? { title: splitTitle.trim() } : {}) })), onSuccess: issue => { saved(issue); navigate(`/issues/${issue.id}`); } });
  useEffect(() => { setPage(1); setActivityPage(1); setSelected([]); setSplitTitle(''); setMergeOpen(false); }, [id]);
  useEffect(() => { if (query.data?.redirected_from) navigate(`/issues/${query.data.id}`, { replace: true }); }, [query.data, navigate]);
  const issue = query.data;
  return <><Link className="back-link" to="/issues">All issues</Link><ErrorNotice error={query.error} />{query.isPending ? <Loading /> : issue && <>
    <PageHeading eyebrow={`ISSUE / ${issue.id.slice(0, 8).toUpperCase()}`} title={issue.title} description={`${issue.repository_name} · ${issue.total_count} uploaded occurrences`} action={<button className="button" onClick={() => setMergeOpen(value => !value)} aria-expanded={mergeOpen}>{mergeOpen ? 'Close merge' : 'Merge issues'}</button>} />
    <div className="run-summary"><Badge status={issue.status} /><span>{issue.owner || 'Unassigned'}</span><span>First: {issue.first_seen ? time(issue.first_seen) : 'Unknown'}</span><span>Last: {issue.last_seen ? time(issue.last_seen) : 'Unknown'}</span></div>
    <Notice>Uploaded data only{issue.tracking_since ? ` · Tracking since ${time(issue.tracking_since)}` : ''}. Duplicate uploads share occurrences. Resolution is managed independently of diagnosis; newer occurrences reopen resolved issues.</Notice>
    <Triage key={issue.id} issue={issue} onSaved={saved} />{mergeOpen && <MergeIssues issue={issue} onSaved={saved} />}
    <Panel title="Occurrences and evidence" subtitle="All captured occurrences, including those outside the default analytics window.">
      <ErrorNotice error={occurrences.error || split.error} />{occurrences.isPending ? <Loading /> : occurrences.data?.items.length ? <div>{occurrences.data.items.map(item => <Occurrence key={item.id} item={item} selected={selected.includes(item.id)} onSelect={checked => setSelected(previous => checked ? [...previous, item.id] : previous.filter(value => value !== item.id))} />)}</div> : <Empty title="No occurrences" action={false} />}
      {!!selected.length && <div className="issue-split"><label>New issue title<input aria-label="Split issue title" placeholder={`Split from ${issue.title}`} maxLength={200} value={splitTitle} onChange={e => setSplitTitle(e.target.value)} /></label><p className="subtle">Only selected occurrences move. Future errors continue using automatic grouping.</p><div className="button-group"><button className="button" disabled={split.isPending || selected.length >= issue.total_count || selected.length > 1000} onClick={() => split.mutate()}>Split {selected.length} selected occurrences</button><button className="button" onClick={() => setSelected([])}>Clear selection</button></div></div>}
      {occurrences.data && <IssuePagination page={page} total={occurrences.data.total} pageSize={occurrences.data.page_size} onPage={setPage} />}
    </Panel>
    <Panel title="Issue activity" subtitle="Triage changes, recurrence, and grouping corrections"><ErrorNotice error={activity.error} />{activity.isPending ? <Loading /> : <ol className="issue-activity">{activity.data?.items.map(item => <li key={item.id}><div><strong>{item.kind[0].toUpperCase() + item.kind.slice(1)}</strong><span>{time(item.created_at)}</span></div><ActivityDetails item={item} /></li>)}</ol>}{activity.data && <IssuePagination page={activityPage} total={activity.data.total} pageSize={activity.data.page_size} onPage={setActivityPage} />}</Panel>
  </>}</>;
}
