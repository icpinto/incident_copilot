import { AlertCircle, ArrowDownToLine, ArrowUpRight, Ban, Check, CheckCircle2, Clock3, FileSearch, Info, LoaderCircle, Plus, Search, TriangleAlert, XCircle, type LucideIcon } from 'lucide-react';
import { Link } from 'react-router-dom';
import { useId, type ReactNode } from 'react';
import type { Incident } from './types';

export const time = (date: string) => new Date(date).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
export function duration(seconds: number) { return seconds < 60 ? `${Math.round(seconds)}s` : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`; }
export const elapsed = (start: string, end?: string | null) => Math.max(0, (new Date(end || Date.now()).getTime() - new Date(start).getTime()) / 1000);

export function Badge({ status }: { status: string }) {
  const icons: Record<string, LucideIcon> = { completed: CheckCircle2, resolved: CheckCircle2, running: LoaderCircle, investigating: Search, queued: Clock3, open: AlertCircle, acknowledged: Clock3, failed: XCircle, canceled: Ban, ignored: Ban };
  const Icon = icons[status];
  return <span className={`badge ${status}`}>{Icon ? <Icon aria-hidden="true" size={13} className={status === 'running' ? 'spin' : undefined} /> : <span className="status-dot" />}{status.replaceAll('_', ' ')}</span>;
}

export function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  return <div className="notice error" role="alert"><AlertCircle size={17} /><span>{error instanceof Error ? error.message : String(error)}</span></div>;
}

export function Notice({ children, tone = 'info' }: { children: ReactNode; tone?: 'info' | 'success' | 'warning' }) {
  const Icon = tone === 'success' ? CheckCircle2 : tone === 'warning' ? TriangleAlert : Info;
  return <div className={`notice ${tone}`} role="status"><Icon aria-hidden="true" size={18} /><div className="notice-content">{children}</div></div>;
}
export function Loading() { return <div className="loading" role="status" aria-label="Loading workspace"><span className="loading-icon"><LoaderCircle aria-hidden="true" className="spin" size={24} /></span><span>Loading workspace…</span></div>; }

export function Empty({ title = 'No incidents yet', description = 'Run your first analysis to turn error logs into actionable evidence.', action = true }: { title?: string; description?: string; action?: boolean }) {
  return <div className="empty"><div className="empty-icon"><FileSearch size={29} strokeWidth={1.3} /></div><h3>{title}</h3><p>{description}</p>{action && <Link className="button primary" to="/incidents/new"><Plus size={16} />New analysis</Link>}</div>;
}

export function PageHeading({ eyebrow, title, description, action }: { eyebrow: string; title: string; description: string; action?: ReactNode }) {
  return <header className="page-heading"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{description}</p></div>{action}</header>;
}

export function Panel({ title, subtitle, children, action, className = '', icon: Icon }: { title: string; subtitle?: string; children: ReactNode; action?: ReactNode; className?: string; icon?: LucideIcon }) {
  const headingId = useId();
  return <section className={`panel ${className}`} aria-labelledby={headingId}><div className="panel-heading"><div className="panel-title-group">{Icon && <span className="panel-symbol"><Icon aria-hidden="true" size={18} /></span>}<div><h2 id={headingId}>{title}</h2>{subtitle && <p>{subtitle}</p>}</div></div>{action}</div>{children}</section>;
}

export function IncidentTable({ rows }: { rows: Incident[] }) {
  return <div className="table-scroll" role="region" aria-label="Investigation table" tabIndex={0}><table><caption className="sr-only">Incidents and their investigation status</caption><thead><tr><th scope="col">Incident</th><th scope="col">Status</th><th scope="col">Repository / Service</th><th scope="col">Engine</th><th scope="col">Created</th><th scope="col"><span className="sr-only">Open incident</span></th></tr></thead><tbody>{rows.map(run => <tr key={run.id}><td><Link className="incident-title" to={`/incidents/${run.id}`}>{run.title}</Link><span className="mono subtle">{run.id.slice(0, 8)} · {run.report?.likely_error_class || 'Awaiting analysis'}</span></td><td><Badge status={run.status} /></td><td><span>{run.repository_name}</span><small>{run.report?.likely_service || '—'}</small></td><td><span className="engine-label">{run.report?.engine === 'deterministic+llm' ? 'LLM assisted' : run.report ? 'Deterministic' : '—'}</span></td><td className="nowrap muted">{time(run.created_at)}</td><td><Link className="icon-button" aria-label={`Open ${run.title}`} to={`/incidents/${run.id}`}><ArrowUpRight size={17} /></Link></td></tr>)}</tbody></table></div>;
}

const stages = [['indexing', 'Repository indexing', 'Discover services, files, and symbols'], ['correlating', 'Evidence correlation', 'Match log signals to source locations'], ['analyzing', 'Deep analysis', 'Inspect the strongest candidates'], ['reasoning', 'Diagnosis', 'Rank hypotheses and next checks']];

export function Pipeline({ run }: { run: Incident }) {
  return <ol className="pipeline">{stages.map(([id, label, description], index) => {
    const event = run.timeline.find(e => e.stage === id);
    const done = !!event?.completed_at;
    const active = run.stage === id && run.status === 'running';
    const failed = run.stage === id && run.status === 'failed';
    return <li key={id} className={done ? 'done' : active ? 'current' : failed ? 'broken' : ''}><div className="stage-icon">{done ? <Check size={15} /> : failed ? <AlertCircle size={15} /> : active ? <LoaderCircle className="spin" size={15} /> : <span>{index + 1}</span>}</div><div><strong>{label}</strong><small>{description}</small></div><span className="mono stage-time">{event ? duration(elapsed(event.started_at, event.completed_at || run.completed_at)) : '—'}{event && event.attempt > 1 && <small>Attempt {event.attempt}</small>}</span></li>;
  })}</ol>;
}

export function ExportLinks({ id }: { id: string }) {
  return <div className="button-group"><a className="button" href={`/api/incidents/${id}/export?format=json`}><ArrowDownToLine size={15} />JSON</a><a className="button" href={`/api/incidents/${id}/export?format=markdown`}><ArrowDownToLine size={15} />Markdown</a></div>;
}
