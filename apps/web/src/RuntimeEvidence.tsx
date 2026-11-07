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
  return <Panel title="Runtime collection" subtitle="Passive observations. Snapshot health describes collection time; incomplete telemetry leaves evidence gaps.">
    <ul>{collection.map(item => <li key={item.id}><strong>{item.capability}</strong>: {item.status}{item.reason && ` · ${item.reason}`} · {Math.round(item.duration_ms)} ms</li>)}</ul>
    {histories.map(row => <details key={`${row.evidence_id}:${row.container_id}`}><summary>{row.service} resource history ({row.samples.length} samples)</summary>
      {row.summary && <p>Captured samples: {row.summary.sample_count ?? row.samples.length} · Peak CPU: {typeof row.summary.peak_cpu?.cpu_percent === 'number' ? `${row.summary.peak_cpu.cpu_percent.toFixed(1)}%` : 'Unavailable'} · Peak memory: {typeof row.summary.peak_memory?.memory_bytes === 'number' ? `${(row.summary.peak_memory.memory_bytes / 1048576).toFixed(1)} MiB` : 'Unavailable'}</p>}
      <div className="table-scroll"><table><caption>Observed CPU and memory for {row.service}</caption><thead><tr><th>Time</th><th>CPU</th><th>Memory / limit</th></tr></thead><tbody>{row.samples.map(point => <tr key={point.timestamp}><td>{new Date(point.timestamp).toLocaleTimeString()}</td><td>{point.cpu_percent == null ? 'Unavailable' : `${point.cpu_percent.toFixed(1)}%`}</td><td>{(point.memory_bytes / 1048576).toFixed(1)} MiB / {point.memory_limit_bytes ? `${(point.memory_limit_bytes / 1048576).toFixed(1)} MiB` : 'Unavailable'}</td></tr>)}</tbody></table></div>
    </details>)}
    {traces.filter(trace => trace.spans.length).map(trace => <details key={trace.id}><summary>Observed trace spans ({trace.spans.length}, sampled)</summary>
      <div className="table-scroll"><table><caption>Mapped service spans from observed incident traces</caption><thead><tr><th>Service</th><th>Operation</th><th>Duration</th><th>Status</th><th>Trace ID</th></tr></thead><tbody>{trace.spans.map((span, index) => <tr key={index}><td>{span.service}</td><td>{span.name}</td><td>{span.duration_ms.toFixed(1)} ms</td><td>{span.error ? 'Error observed' : 'No span error reported'}</td><td className="mono">{span.trace_id}</td></tr>)}</tbody></table></div>
    </details>)}
  </Panel>;
}
