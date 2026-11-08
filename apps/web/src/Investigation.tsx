import { Panel } from './components';
import type { EvidenceItem, InvestigationEvent, InvestigationSummary, ReasoningSummary } from './types';

const readable = (value: string) => value.replaceAll('_', ' ');
const evidenceAnchor = (id: string) => `retrieved-evidence-${id}`;

export function Investigation({ summary, evidence, events }: {
  summary?: InvestigationSummary | null;
  evidence: EvidenceItem[];
  events: InvestigationEvent[];
}) {
  if (!summary && !evidence.length && !events.length) return null;
  const knownIds = new Set(evidence.map(item => item.id));
  return <Panel title="Evidence investigation" subtitle="Citations identify collected observations. Source relationships and runtime association alone do not establish causation.">
    {summary && <>
      <dl className="investigation-summary">
        <div><dt>Status</dt><dd>{summary.incomplete ? 'Incomplete' : 'Complete'}</dd></div>
        <div><dt>Stopped</dt><dd>{readable(summary.stop_reason)}</dd></div>
        <div><dt>Retrieval rounds</dt><dd>{summary.rounds}</dd></div>
        <div><dt>Model calls</dt><dd>{summary.model_calls}</dd></div>
        <div><dt>Evidence requests</dt><dd>{summary.tool_calls}</dd></div>
        <div><dt>Duration</dt><dd>{(summary.elapsed_ms / 1000).toFixed(1)} seconds</dd></div>
      </dl>
      {!!summary.unresolved_gaps.length && <div><h3>Unresolved evidence gaps</h3><ul>{summary.unresolved_gaps.map((gap, i) => <li key={i}>{gap}</li>)}</ul></div>}
    </>}
    {!!events.length && <details className="deep-analysis"><summary>Investigation actions ({events.length})</summary><ol className="investigation-actions">{events.map(event => <li key={event.id}>
      <strong>{event.tool ? readable(event.tool) : readable(event.kind)}</strong> <span>{readable(event.status)} · {event.duration_ms} ms</span>
      {event.question && <p>{event.question}</p>}{event.reason && <p>{event.reason}</p>}
      {event.evidence_ids.filter(id => knownIds.has(id)).map(id => <a className="evidence-link" href={`#${evidenceAnchor(id)}`} key={id}>{id}</a>)}
    </li>)}</ol></details>}
    {!!evidence.length && <div className="retrieved-evidence"><h3>Retrieved evidence ({evidence.length})</h3>{evidence.map(item => <article className="retrieved-evidence-item" id={evidenceAnchor(item.id)} key={item.id}>
      <details><summary><code>{item.id}</code> {item.file_path || readable(item.kind)}{item.line_start != null && `:${item.line_start}${item.line_end != null ? `–${item.line_end}` : ''}`}</summary>
        {item.runtime ? <><p>{item.runtime.collector_id} / {item.runtime.project} / {item.runtime.service} · {item.runtime.environment}</p><p>Requested: {item.runtime.requested_start} – {item.runtime.requested_end}</p><p>Observed: {item.runtime.observed_at} · Coverage: {item.runtime.coverage}</p></> : <p className="mono subtle">Revision: {item.revision || 'unavailable'}</p>}
        {!!item.limitations.length && <p>Limitations: {item.limitations.join('; ')}</p>}
        {item.truncated && <p>Content clipped. Additional evidence may remain unread.</p>}
        <pre className="code-block"><code>{item.content || JSON.stringify(item.data, null, 2)}</code></pre>
      </details>
    </article>)}</div>}
  </Panel>;
}

export function ReasoningDetails({ summary }: { summary?: ReasoningSummary | null }) {
  if (!summary) return null;
  return <Panel title="Diagnosis reasoning" subtitle="Causal explanations are assessed against collected evidence.">
    <dl className="investigation-summary">
      <div><dt>Approach</dt><dd>{readable(summary.mode)}</dd></div>
      <div><dt>Evidence review</dt><dd>{readable(summary.review_status)}</dd></div>
      <div><dt>Diagnosis model calls</dt><dd>{summary.model_calls}</dd></div>
    </dl>
    {!!summary.review_reasons.length && <p>Review reasons: {summary.review_reasons.map(readable).join('; ')}</p>}
    {summary.review_failure && <p>Review limitation: {readable(summary.review_failure)}. The latest valid diagnosis is retained.</p>}
  </Panel>;
}
