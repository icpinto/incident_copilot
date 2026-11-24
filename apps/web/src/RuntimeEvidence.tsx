import { Activity, CheckCircle2, Circle, TriangleAlert } from 'lucide-react';
import { Panel } from './components';
import type { EvidenceItem, RuntimeCollection } from './types';

type Point = { timestamp: string; cpu_percent: number | null; memory_bytes: number; memory_limit_bytes?: number };
type History = { service: string; container_id: string; samples: Point[]; summary?: { sample_count?: number; peak_cpu?: Point | null; peak_memory?: Point | null } };
type Span = { service: string; name: string; trace_id: string; duration_ms: number; error: boolean };

export function RuntimeEvidence({ evidence, collection }: { evidence: EvidenceItem[]; collection: RuntimeCollection[] }) {
  if (!collection.length && !evidence.length) return null;
  const histories = evidence.filter(item => item.kind === 'runtime_resources').flatMap(item => {
    if (!Array.isArray(item.data.observations)) return [];
    return item.data.observations.filter((row): row is History => typeof row === 'object' && row !== null && typeof row.service === 'string' && typeof row.container_id === 'string' && Array.isArray(row.samples))
      .map(row => ({ ...row, evidence_id: item.id, samples: row.samples.filter(point => point !== null && typeof point === 'object' && typeof point.timestamp === 'string' && typeof point.memory_bytes === 'number' && (point.cpu_percent === null || typeof point.cpu_percent === 'number')) }));
  });
  const traces = evidence.filter(item => item.kind === 'runtime_traces').map(item => ({ id: item.id, spans: (Array.isArray(item.data.spans) ? item.data.spans : []).filter((span): span is Span => typeof span === 'object' && span !== null && typeof span.service === 'string' && typeof span.name === 'string' && typeof span.trace_id === 'string' && typeof span.duration_ms === 'number') }));
  return <Panel icon={Activity} className="runtime-panel" title="Runtime collection" subtitle="Passive observations. Snapshot health describes collection time; incomplete telemetry leaves evidence gaps.">
    {!!collection.length && <ul className="runtime-collection">{collection.map(item => <li key={item.id}>
      <div className="runtime-capability"><strong>{item.capability}</strong><span className={`runtime-status ${item.status === 'ok' ? 'is-success' : ['partial', 'unavailable'].includes(item.status) ? 'is-warning' : ''}`}>{item.status === 'ok' ? <CheckCircle2 aria-hidden="true" size={13} /> : ['partial', 'unavailable'].includes(item.status) ? <TriangleAlert aria-hidden="true" size={13} /> : <Circle aria-hidden="true" size={12} />}{item.status}</span></div>
      {item.reason && <p>{item.reason}</p>}<small>{Math.round(item.duration_ms)} ms</small>
    </li>)}</ul>}
    {histories.map(row => <details className="runtime-detail" key={`${row.evidence_id}:${row.container_id}`}><summary>{row.service} resource history ({row.samples.length} samples)</summary>
      <p className="mono runtime-container">Container: {row.container_id}</p>
      {row.summary && <p>Captured samples: {row.summary.sample_count ?? row.samples.length} · Peak CPU: {typeof row.summary.peak_cpu?.cpu_percent === 'number' ? `${row.summary.peak_cpu.cpu_percent.toFixed(1)}%` : 'Unavailable'} · Peak memory: {typeof row.summary.peak_memory?.memory_bytes === 'number' ? `${(row.summary.peak_memory.memory_bytes / 1048576).toFixed(1)} MiB` : 'Unavailable'}</p>}
      <div className="table-scroll" role="region" aria-label={`Resource history for ${row.service}, container ${row.container_id}`} tabIndex={0}><table><caption>Observed CPU and memory for {row.service}</caption><thead><tr><th scope="col">Time</th><th scope="col">CPU</th><th scope="col">Memory / limit</th></tr></thead><tbody>{row.samples.map(point => <tr key={point.timestamp}><td>{new Date(point.timestamp).toLocaleTimeString()}</td><td>{point.cpu_percent == null ? 'Unavailable' : `${point.cpu_percent.toFixed(1)}%`}</td><td>{(point.memory_bytes / 1048576).toFixed(1)} MiB / {point.memory_limit_bytes ? `${(point.memory_limit_bytes / 1048576).toFixed(1)} MiB` : 'Unavailable'}</td></tr>)}</tbody></table></div>
    </details>)}
    {traces.filter(trace => trace.spans.length).map(trace => <details className="runtime-detail" key={trace.id}><summary>Observed trace spans ({trace.spans.length}, sampled)</summary>
      <div className="table-scroll" role="region" aria-label={`Observed trace spans for ${trace.id}`} tabIndex={0}><table><caption>Mapped service spans from observed incident traces</caption><thead><tr><th scope="col">Service</th><th scope="col">Operation</th><th scope="col">Duration</th><th scope="col">Status</th><th scope="col">Trace ID</th></tr></thead><tbody>{trace.spans.map((span, index) => <tr key={index}><td>{span.service}</td><td>{span.name}</td><td>{span.duration_ms.toFixed(1)} ms</td><td><span className={`runtime-status${span.error ? ' is-warning' : ''}`}>{span.error ? 'Error observed' : 'No span error reported'}</span></td><td className="mono">{span.trace_id}</td></tr>)}</tbody></table></div>
    </details>)}
  </Panel>;
}
