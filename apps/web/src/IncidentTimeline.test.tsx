import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { IncidentTimeline, OriginalLogs } from './IncidentTimeline';
import type { IncidentContext, LogEvent } from './types';

function event(index: number, timestamp: string | null = null): LogEvent {
  return { id: `event-${index}`, line_start: index, line_end: index, message: `failure ${index}`,
    service: 'api', timestamp, timestamp_utc: timestamp, trace_id: 'shared', request_id: null,
    error_bearing: true, exceptions: [], stack_frames: [], limitations: [] };
}

function context(events: LogEvent[]): IncidentContext {
  return { version: 1, events, groups: [{ id: 'primary', event_ids: events.map(e => e.id), services: ['api'],
    first_line: 1, grouping_basis: 'trace_id', error_bearing: true, max_candidate_score: 32 }],
    primary_group_id: 'primary', selection_reason: 'Selected highest candidate score.', limitations: [] };
}

describe('incident timeline', () => {
  it('labels primary and excluded groups and links to original source lines', async () => {
    const value = context([event(1)]);
    value.events.push({ ...event(2), service: 'worker', exceptions: [{ id: 'cause', type: 'TypeError', message: 'invalid', relation: 'cause', previous_id: 'original', stack_frames: [] }] });
    value.groups.push({ ...value.groups[0], id: 'secondary', event_ids: ['event-2'], services: ['worker'] });
    const onSourceRange = vi.fn();
    render(<IncidentTimeline context={value} onSourceRange={onSourceRange} />);
    expect(screen.getByText('Primary group')).toBeInTheDocument();
    expect(screen.getByText('Context only')).toBeInTheDocument();
    expect(screen.getByText('Selected highest candidate score.')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('link', { name: 'Original lines 1–1' }));
    expect(onSourceRange).toHaveBeenCalledWith(1, 1);
    await userEvent.click(screen.getByText('secondary'));
    expect(screen.getByText('TypeError')).toBeVisible();
    expect(screen.getByText(/cause \(original\)/)).toBeVisible();
  });

  it('starts with 100 events and shows more on demand', async () => {
    render(<IncidentTimeline context={context(Array.from({ length: 102 }, (_, index) => event(index + 1)))} onSourceRange={() => {}} />);
    const list = screen.getByRole('list', { name: 'Events in primary' });
    expect(within(list).getAllByRole('listitem')).toHaveLength(100);
    expect(screen.queryByText('failure 101')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Show more events/ }));
    expect(within(list).getAllByRole('listitem')).toHaveLength(102);
    expect(screen.queryByRole('button', { name: /Show more events/ })).not.toBeInTheDocument();
  });

  it('keeps source order by default and sorts comparable times stably', async () => {
    render(<IncidentTimeline context={context([
      event(1, '2026-10-26T10:00:02Z'), event(2, '2026-10-26T10:00:01Z'), event(3, '2026-10-26T10:00:01Z'),
    ])} onSourceRange={() => {}} />);
    const list = screen.getByRole('list', { name: 'Events in primary' });
    expect(within(list).getAllByRole('listitem').map(e => e.id)).toEqual(['log-event-event-1', 'log-event-event-2', 'log-event-event-3']);
    await userEvent.click(screen.getByRole('checkbox', { name: 'Order by timestamp' }));
    expect(within(list).getAllByRole('listitem').map(e => e.id)).toEqual(['log-event-event-2', 'log-event-event-3', 'log-event-event-1']);
  });

  it('does not offer chronological sorting with missing timestamps', () => {
    render(<IncidentTimeline context={context([event(1), event(2, '2026-10-26T10:00:01Z')])} onSourceRange={() => {}} />);
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    expect(screen.getByText(/Some timestamps cannot be compared/)).toBeInTheDocument();
  });

  it('shows the selected original range and retains the full input', () => {
    render(<OriginalLogs text={'first\nsecond\nthird'} range={[2, 2]} />);
    expect(screen.getByText('2: second')).toBeInTheDocument();
    expect(screen.getByText('first second third')).toBeInTheDocument();
  });
});
