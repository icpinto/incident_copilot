import { useState } from 'react';
import { Panel } from './components';
import type { IncidentContext, IncidentGroup, LogEvent } from './types';

type SourceRange = (start: number, end: number) => void;

function EventGroup({ group, events, primary, onSourceRange }: {
  group: IncidentGroup; events: LogEvent[]; primary: boolean; onSourceRange: SourceRange;
}) {
  const [limit, setLimit] = useState(100);
  const [chronological, setChronological] = useState(false);
  const comparable = events.length > 0 && events.every(e => e.timestamp_utc && Number.isFinite(Date.parse(e.timestamp_utc)));
  const ordered = chronological && comparable
    ? [...events].sort((a, b) => Date.parse(a.timestamp_utc!) - Date.parse(b.timestamp_utc!) || a.line_start - b.line_start)
    : events;
  return <details className="incident-event-group" open={primary}>
    <summary><code>{group.id}</code> <span className="engine-chip">{primary ? 'Primary group' : 'Context only'}</span>
      <span>{group.services.join(', ') || 'Unknown service'} · {events.length} events · {group.grouping_basis.replace('_', ' ')}</span>
    </summary>
    {comparable ? <label className="timeline-order"><input type="checkbox" checked={chronological} onChange={e => setChronological(e.target.checked)} />Order by timestamp</label>
      : <p>Source order shown. Some timestamps cannot be compared.</p>}
    <ol className="incident-events" aria-label={`Events in ${group.id}`}>
      {ordered.slice(0, limit).map(event => <li key={event.id} id={`log-event-${event.id}`}>
        <div className="event-metadata"><strong>{event.service || 'Unknown service'}</strong>
          <span>{event.timestamp || 'No timestamp'}{event.timestamp && !event.timestamp_utc ? ' (timezone unknown or timestamp invalid)' : ''}</span>
          <a href="#incident-log-source" onClick={() => onSourceRange(event.line_start, event.line_end)}>Original lines {event.line_start}–{event.line_end}</a>
        </div>
        {(event.trace_id || event.request_id) && <p className="mono">{event.trace_id && `Trace: ${event.trace_id} `}{event.request_id && `Request: ${event.request_id}`}</p>}
        <pre className="code-block">{event.message}</pre>
        {!!event.exceptions.length && <ul aria-label="Exception chain">{event.exceptions.map(exception => <li key={exception.id}>
          <code>{exception.type}</code>: {exception.message}
          {exception.relation && <span> · {exception.relation.replace('_', ' ')} ({exception.previous_id})</span>}
        </li>)}</ul>}
        {!!event.limitations.length && <ul className="subtle">{event.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul>}
      </li>)}
    </ol>
    {limit < events.length && <button className="button" onClick={() => setLimit(value => value + 100)}>Show more events ({events.length - limit} remaining)</button>}
  </details>;
}

export function IncidentTimeline({ context, onSourceRange }: { context: IncidentContext; onSourceRange: SourceRange }) {
  const eventMap = new Map(context.events.map(event => [event.id, event]));
  return <Panel title="Incident timeline" subtitle="Diagnosis covers the primary group. Other groups are available for inspection.">
    <div className="incident-timeline">
      <p>{context.selection_reason || 'Primary group has not been selected.'}</p>
      <p className="subtle">Shared identifiers and chronology establish association, not causation. Source order is the default.</p>
      {!!context.limitations.length && <ul>{context.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul>}
      {context.groups.map(group => <EventGroup key={group.id} group={group} primary={group.id === context.primary_group_id}
        events={group.event_ids.map(id => eventMap.get(id)).filter((event): event is LogEvent => !!event)} onSourceRange={onSourceRange} />)}
    </div>
  </Panel>;
}

export function OriginalLogs({ text, range }: { text: string; range: [number, number] | null }) {
  const lines = range ? text.split('\n') : [];
  return <Panel title="Original incident logs" subtitle="The input captured for this investigation">
    <div id="incident-log-source" className="original-log-source" tabIndex={-1}>
      {range && <div className="source-range"><strong>Selected original lines {range[0]}–{range[1]}</strong>
        <pre className="code-block">{lines.slice(range[0] - 1, Math.min(range[1], range[0] + 199)).map((line, i) => `${range[0] + i}: ${line}`).join('\n')}</pre>
        {range[1] - range[0] >= 200 && <p>Preview limited to 200 lines. The complete input is below.</p>}
      </div>}
      <pre className="code-block logs-block">{text || 'No logs captured.'}</pre>
    </div>
  </Panel>;
}
