import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { BookOpen, Copy, LoaderCircle, Save } from 'lucide-react';
import { api, json } from './api';
import { ErrorNotice, Notice, Panel, time } from './components';
import type { ActionEvent, ActionStatus, EvidenceItem, Hypothesis, NextCheck, RunbookSnapshot } from './types';

const readable = (value: string) => value.replaceAll('_', ' ');
const sectionId = (heading: string) => heading.toLowerCase().replace(/[^a-z0-9]+/g, '-');
function revealRunbook(id: string) {
  const element = document.getElementById(`runbook-${id}`);
  if (element instanceof HTMLDetailsElement) element.open = true;
}
const checkStatuses: ActionStatus[] = ['supports', 'weakens', 'inconclusive', 'blocked', 'skipped'];
const mitigationStatuses: ActionStatus[] = ['in_progress', 'applied', 'verified', 'failed', 'rolled_back', 'skipped'];

function CopyInstruction({ text }: { text: string }) {
  const [message, setMessage] = useState('');
  return <><button type="button" className="button" onClick={async () => {
    try { await navigator.clipboard.writeText(text); setMessage('Copied'); }
    catch { setMessage('Copy unavailable. Select and copy the instructions.'); }
  }}><Copy aria-hidden="true" size={14} />Copy instructions</button><span role="status">{message}</span></>;
}

function ActionOutcome({ incidentId, actionId, kind, blocked = false }: {
  incidentId: string; actionId: string; kind: 'check' | 'mitigation'; blocked?: boolean;
}) {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ['action-events', incidentId],
    queryFn: () => api<ActionEvent[]>(`/incidents/${incidentId}/action-events`) });
  const [status, setStatus] = useState<ActionStatus>(kind === 'check' ? 'inconclusive' : 'in_progress');
  const [observations, setObservations] = useState('');
  const [responder, setResponder] = useState('');
  const [useful, setUseful] = useState('');
  // Preserve an identifier across network retries; edits intentionally create a new event.
  const submission = useRef<{ key: string; id: string } | null>(null);
  const mutation = useMutation({ mutationFn: () => {
    const value = { action_id: actionId, status, observations, responder: responder.trim() || null, useful: useful === '' ? null : useful === 'true' };
    const key = JSON.stringify(value);
    if (submission.current?.key !== key) submission.current = { key, id: crypto.randomUUID() };
    return api<ActionEvent>(`/incidents/${incidentId}/action-events`, json('POST', {
      ...value, client_event_id: submission.current.id,
    }));
  }, onSuccess: event => {
    client.setQueryData<ActionEvent[]>(['action-events', incidentId], old =>
      [...(old || []).filter(item => item.client_event_id !== event.client_event_id), event]
        .sort((left, right) => left.recorded_at.localeCompare(right.recorded_at)));
    client.invalidateQueries({ queryKey: ['action-events', incidentId] });
    setObservations(''); submission.current = null;
  } });
  const history = Array.isArray(query.data) ? query.data.filter(item => item.action_id === actionId) : [];
  const latest = history.at(-1);
  const needsObservations = status === 'verified' || (blocked && ['in_progress', 'applied'].includes(status));
  return <section className="action-outcome" aria-label={`Outcome for ${actionId}`}>
    <h4>Responder outcome: {latest ? readable(latest.status) : kind === 'check' ? 'Not checked' : 'Not attempted'}</h4>
    {latest && <p>{latest.observations || 'No observations recorded'} <small>{time(latest.recorded_at)} - {latest.responder || 'Responder not specified'}</small></p>}
    <details><summary>Record outcome</summary>
      <p className="subtle">Responder-reported results are saved for handoff. The diagnosis stays unchanged.</p>
      <form aria-busy={mutation.isPending} onSubmit={event => { event.preventDefault(); mutation.mutate(); }}>
        <label>Status<select value={status} onChange={event => setStatus(event.target.value as ActionStatus)}>
          {(kind === 'check' ? checkStatuses : mitigationStatuses).map(value => <option key={value} value={value}>{readable(value)}</option>)}
        </select></label>
        <label className="outcome-observations">Observations{needsObservations && ' (required)'}<textarea value={observations} required={needsObservations} maxLength={8000}
          onChange={event => setObservations(event.target.value)} placeholder={status === 'verified' ? 'Record the measurements, success condition and observation period.' : blocked ? 'Record results and how missing prerequisites were satisfied.' : 'What did you observe?'} /></label>
        <label>Responder (optional, self-reported)<input maxLength={120} value={responder} onChange={event => setResponder(event.target.value)} /></label>
        <label>Was this recommendation useful?<select value={useful} onChange={event => setUseful(event.target.value)}>
          <option value="">Not rated</option><option value="true">Useful</option><option value="false">Not useful</option>
        </select></label>
        <button className="button" disabled={mutation.isPending || (needsObservations && !observations.trim())}>{mutation.isPending ? <LoaderCircle aria-hidden="true" className="spin" size={14} /> : <Save aria-hidden="true" size={14} />}{mutation.isPending ? 'Saving...' : 'Save outcome'}</button>
        {mutation.isSuccess && <span role="status">Outcome saved</span>}
      </form>
    </details>
    <ErrorNotice error={query.error || mutation.error} />
    {!!history.length && <details><summary>Outcome history ({history.length})</summary><ol>{history.map(event => <li key={event.client_event_id}>
      <strong>{readable(event.status)}</strong> · {time(event.recorded_at)} · {event.responder || 'Responder not specified'}<p>{event.observations || 'No observations recorded'}</p>
    </li>)}</ol></details>}
  </section>;
}

function Check({ check, incidentId }: { check: NextCheck; incidentId: string }) {
  return <>
    <p><strong>Where:</strong> {check.context}</p>
    {check.command && <pre className="code-block"><code>{check.command}</code></pre>}
    {!!check.placeholders.length && <Notice tone="warning">Resolve placeholders before use: {check.placeholders.join('; ')}</Notice>}
    <p><strong>Collect:</strong> {check.evidence_to_collect}</p>
    <h4>Expected results (predictions)</h4>
    <p><strong>Supports this hypothesis:</strong> {check.supports_hypothesis}</p>
    <p><strong>Weakens this hypothesis:</strong> {check.weakens_hypothesis}</p>
    <CopyInstruction text={[check.instruction, `Where: ${check.context}`, check.command,
      check.placeholders.length ? `Resolve placeholders: ${check.placeholders.join('; ')}` : '',
      `Collect: ${check.evidence_to_collect}`].filter(Boolean).join('\n')} />
    <ActionOutcome incidentId={incidentId} actionId={check.id} kind="check" />
  </>;
}

export function HypothesisRecommendations({ hypothesis, incidentId, evidence }: {
  hypothesis: Hypothesis; incidentId: string; evidence: EvidenceItem[];
}) {
  const plan = hypothesis.action_plan;
  if (!plan) return <div className="hypothesis-grid"><div><h3>Supporting evidence</h3><ul>{hypothesis.evidence.map((item, i) => <li key={i}>{item}</li>)}</ul></div><div><h3>Next checks</h3><ol>{hypothesis.next_checks.map((item, i) => <li key={i}>{item}</li>)}</ol></div></div>;
  return <div className="recommendations">
    <h3>Supporting evidence</h3><ul>{plan.supporting_evidence.map((claim, index) => <li key={index}>{claim.claim}{' '}
      {claim.evidence_refs.map(ref => {
        const item = evidence.find(value => value.id === ref);
        return item && <a className="evidence-link" key={ref} href={`#retrieved-evidence-${ref}`}>
          {item.file_path ? `${item.file_path}${item.line_start ? `:${item.line_start}` : ''}` : item.runtime ? `${item.runtime.service} - ${time(item.runtime.observed_at)}` : ref}
        </a>;
      })}</li>)}</ul>
    <h3>Missing evidence</h3>{plan.missing_evidence.length ? <ul>{plan.missing_evidence.map((gap, i) => <li key={i}>{gap}</li>)}</ul> : <p>No additional gaps identified by this assessment; uncertainty remains.</p>}
    <h3>Next checks</h3>{plan.checks.map((check, index) => <details open={index === 0} className="recommendation-check" key={check.id}>
      <summary>{index + 1}. {check.instruction}</summary><Check check={check} incidentId={incidentId} />
    </details>)}
    <h3>Relevant runbooks</h3>{plan.runbooks.length ? <ul>{plan.runbooks.map((ref, index) => <li key={index}>
      <a href={`#runbook-${ref.runbook_id}-${sectionId(ref.section)}`} onClick={() => revealRunbook(ref.runbook_id)}>{ref.runbook_id} - {ref.section}</a>: {ref.reason}
    </li>)}</ul> : <p>No matching runbook.</p>}
    <h3>Mitigation proposals</h3>{!plan.mitigations.length && <p>No mitigation proposed. Complete the evidence checks first.</p>}
    {plan.mitigations.map(mitigation => <section className="mitigation-proposal" key={mitigation.id}>
      <h4>{mitigation.title}</h4><span className="engine-chip">{mitigation.runbook_ids.length ? 'Repository runbook' : 'Unreviewed model proposal'}</span>
      <p>{mitigation.intended_effect}</p>
      <h4>Prerequisites</h4><ul>{mitigation.prerequisites.map((value, i) => <li key={i}>{value}</li>)}</ul>
      {!!mitigation.missing_prerequisites.length && <Notice tone="warning"><strong>Blocked: missing prerequisites</strong><ul>{mitigation.missing_prerequisites.map((value, i) => <li key={i}>{value}</li>)}</ul></Notice>}
      <h4>Risks</h4><ul>{mitigation.risks.map((value, i) => <li key={i}>{value}</li>)}</ul>
      <p><strong>Affected services:</strong> {mitigation.affected_services.join(', ')}</p>
      {!!mitigation.runbook_ids.length && <p>Procedure sources: {mitigation.runbook_ids.map(id => <a className="evidence-link" key={id} href={`#runbook-${id}`} onClick={() => revealRunbook(id)}>{id}</a>)}</p>}
      <h4>Procedure</h4><ol>{mitigation.steps.map((value, i) => <li key={i}>{value}</li>)}</ol>
      <h4>Rollback</h4><ul>{mitigation.rollback.map((value, i) => <li key={i}>{value}</li>)}</ul>
      <h4>Recovery verification (predicted criteria)</h4><dl>
        <dt>Measure</dt><dd>{mitigation.verification.measure}</dd><dt>Success condition</dt><dd>{mitigation.verification.success_condition}</dd>
        <dt>Observation period</dt><dd>{mitigation.verification.observation_period}</dd>
      </dl>
      <CopyInstruction text={[
        mitigation.title, `Prerequisites: ${mitigation.prerequisites.join('; ')}`,
        `Missing prerequisites: ${mitigation.missing_prerequisites.join('; ') || 'None identified'}`,
        `Risks: ${mitigation.risks.join('; ')}`, `Affected services: ${mitigation.affected_services.join(', ')}`,
        ...mitigation.steps.map((step, i) => `${i + 1}. ${step}`), `Rollback: ${mitigation.rollback.join('; ')}`,
        `Verify: ${mitigation.verification.measure}; ${mitigation.verification.success_condition}; ${mitigation.verification.observation_period}`,
      ].join('\n')} />
      <ActionOutcome incidentId={incidentId} actionId={mitigation.id} kind="mitigation" blocked={!!mitigation.missing_prerequisites.length} />
    </section>)}
  </div>;
}

export function RunbookSnapshots({ runbooks }: { runbooks: RunbookSnapshot[] }) {
  if (!runbooks.length) return null;
  return <Panel icon={BookOpen} className="runbook-panel" title="Repository runbook snapshots" subtitle="Saved procedure content from the analysis snapshot.">
    {runbooks.map(book => <details className="runbook-snapshot" id={`runbook-${book.id}`} key={book.id}><summary>{book.title} - {book.file_path}</summary>
      <p>Revision: {book.revision || 'unknown'} · SHA-256: <code>{book.content_hash}</code></p>
      {book.sections.map((section, i) => <section key={i} id={`runbook-${book.id}-${sectionId(section.heading)}`}><h4>{section.heading} · lines {section.line_start}-{section.line_end}</h4>
        <pre className="code-block"><code>{section.content}</code></pre></section>)}
    </details>)}
  </Panel>;
}
