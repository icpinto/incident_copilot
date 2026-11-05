import { Panel } from './components';
import type { GuardrailSummary } from './types';

const readable = (value: string) => value.replaceAll('_', ' ');

export function Guardrails({ summary }: { summary?: GuardrailSummary | null }) {
  if (!summary) return null;
  const findings = summary.events.filter(event => event.status === 'flagged').length;
  const failures = summary.events.filter(event => event.status === 'error' || event.status === 'skipped').length;
  return <div>
    <Panel title="Guardrails" subtitle={summary.mode === 'observe'
      ? 'Observation mode records findings and check failures without blocking analysis.'
      : 'Enforcement stops a model step when content is rejected or a check is unavailable.'}>
      <dl className="investigation-summary">
        <div><dt>Mode</dt><dd>{{ off: 'Off', observe: 'Observe', enforce: 'Enforce' }[summary.mode]}</dd></div>
        <div><dt>Policy findings</dt><dd>{findings}</dd></div>
        <div><dt>Check failures</dt><dd>{failures}</dd></div>
        <div><dt>Checks</dt><dd>{summary.checks}</dd></div>
        <div><dt>Additional model calls</dt><dd>{summary.model_calls}</dd></div>
        <div><dt>Check duration</dt><dd>{(summary.duration_ms / 1000).toFixed(2)} seconds</dd></div>
      </dl>
      <details className="deep-analysis"><summary>Policy check details</summary>
        <p>Policy: <code>{summary.policy_version}</code></p>
        <ul>{summary.events.map(event => <li key={event.id}>
          {readable(event.stage)} · Assessment {event.model_call} · {readable(event.status)} · {readable(event.code)} · {event.duration_ms.toFixed(0)} ms
        </li>)}</ul>
      </details>
    </Panel>
  </div>;
}
