import { useQuery } from '@tanstack/react-query';
import { Activity, ChartNoAxesCombined, CircleDot, Flame, ListChecks, Plus, RotateCcw } from 'lucide-react';
import { Link, useNavigate } from 'react-router-dom';
import { Area, AreaChart, Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { api } from '../api';
import { ErrorNotice, Loading, Notice, PageHeading, Panel, time } from '../components';
import { changeLabel, dimensionFilter, IssueFilters, IssueTable, useErrorParams } from '../IssueComponents';
import type { ErrorDimension, ErrorIntelligenceData } from '../types';
import { chartTheme, chartTooltip } from '../chartTheme';

export function ErrorIntelligence() {
  const { params, update } = useErrorParams();
  const navigate = useNavigate();
  const query = useQuery({ queryKey: ['error-intelligence', params.toString()], queryFn: () => api<ErrorIntelligenceData>(`/error-intelligence?${params}`), refetchInterval: 15000 });
  const data = query.data;
  const dimension = (params.get('dimension') || 'service') as ErrorDimension;
  const drill = (value: string) => {
    const next = new URLSearchParams(params); next.set(dimensionFilter[dimension], value); next.delete('dimension'); next.delete('page'); navigate(`/issues?${next}`);
  };
  return <><PageHeading eyebrow="UPLOADED ERROR INTELLIGENCE" title="Error intelligence" description="Find recurring errors, growing issues, and stack hotspots." action={<Link className="button primary" to="/incidents/new"><Plus aria-hidden="true" size={16} />New analysis</Link>} />
    <IssueFilters /><ErrorNotice error={query.error} />{query.isPending ? <Loading /> : data && <>
      <Notice>Uploaded data only · Tracking new submissions since {time(data.tracking_since)}. Exact duplicate uploads and reruns count once.</Notice>
      <div className="metrics-grid">{[
        { label: 'Error occurrences', value: data.summary.occurrences, note: changeLabel(data.summary), icon: Activity },
        { label: 'Active issues', value: data.summary.active_issues, note: `${data.summary.issues} issues observed in this window`, icon: CircleDot },
        { label: 'New issues', value: data.summary.new_issues, note: 'First observed occurrence in this window', icon: Plus },
        { label: 'Reopened issues', value: data.summary.reopened_issues, note: 'New occurrences after resolution', icon: RotateCcw },
      ].map(metric => <section className="metric" aria-label={metric.label} key={metric.label}><div className="metric-top"><span>{metric.label}</span><span className="metric-icon"><metric.icon aria-hidden="true" size={17} /></span></div><strong>{metric.value.toLocaleString()}</strong><small>{metric.note}</small></section>)}</div>
      <p className="subtle issue-coverage">{time(data.start_at)} – {time(data.end_at)} · Compared with the immediately preceding equal-length period. {data.summary.source_timestamp_count} occurrences use event timestamps; {data.summary.fallback_timestamp_count} use original submission time. Daily buckets use UTC.</p>
      <div className="charts-grid"><Panel icon={ChartNoAxesCombined} title="Error occurrences over time" subtitle="Select a day to inspect its issues."><div className="chart" role="img" aria-label="Daily uploaded error occurrences"><ResponsiveContainer width="100%" height="100%"><AreaChart data={data.trends} onClick={state => {
        if (!state.activeLabel) return; const day = String(state.activeLabel); const next = new URLSearchParams(params);
        const start = new Date(`${day}T00:00:00Z`).getTime(); const end = start + 86400000;
        next.set('start_at', new Date(Math.max(start, Date.parse(data.start_at))).toISOString()); next.set('end_at', new Date(Math.min(end, Date.parse(data.end_at))).toISOString()); next.set('window', 'custom'); next.delete('page'); navigate(`/issues?${next}`);
      }}><CartesianGrid stroke={chartTheme.grid} vertical={false} /><XAxis dataKey="date" tick={chartTheme.tick} minTickGap={35} /><YAxis allowDecimals={false} tick={chartTheme.tick} /><Tooltip contentStyle={chartTooltip} /><Area isAnimationActive={false} dataKey="occurrences" name="Occurrences" stroke={chartTheme.accent} fill={chartTheme.accent} fillOpacity={0.12} /></AreaChart></ResponsiveContainer></div>
        <details className="issue-chart-data"><summary>View daily counts and links</summary>{data.trends.map(point => {
          const next = new URLSearchParams(params); const start = Date.parse(`${point.date}T00:00:00Z`);
          next.set('start_at', new Date(Math.max(start, Date.parse(data.start_at))).toISOString()); next.set('end_at', new Date(Math.min(start + 86400000, Date.parse(data.end_at))).toISOString()); next.set('window', 'custom'); next.delete('page');
          return <Link key={point.date} to={`/issues?${next}`}>{point.date}: {point.occurrences}</Link>;
        })}</details>
      </Panel><Panel icon={Flame} title="Error hotspots" subtitle="Observed stack locations and metadata from uploads." action={<select aria-label="Hotspot dimension" value={dimension} onChange={e => update('dimension', e.target.value)}>{(['service', 'repository', 'error_class', 'file', 'function'] as const).map(value => <option key={value} value={value}>{value.replace('_', ' ')}</option>)}</select>}>
        {data.hotspots.length ? <><div className="chart" role="img" aria-label={`Errors by ${dimension}`}><ResponsiveContainer width="100%" height="100%"><BarChart data={data.hotspots.slice(0, 8)} layout="vertical" margin={{ left: 10, right: 25 }}><XAxis type="number" allowDecimals={false} tick={chartTheme.tick} /><YAxis dataKey="label" type="category" width={115} tick={chartTheme.tick} tickFormatter={value => String(value).length > 18 ? `…${String(value).slice(-17)}` : String(value)} /><Tooltip contentStyle={chartTooltip} /><Bar isAnimationActive={false} dataKey="occurrences" fill={chartTheme.secondary} onClick={(_, index) => drill(data.hotspots[index].value)} /></BarChart></ResponsiveContainer></div><div className="hotspot-links">{data.hotspots.map(item => <button className="hotspot-link" key={item.value} onClick={() => drill(item.value)}><span>{item.label}</span><strong>{item.occurrences}</strong></button>)}</div></> : <p className="issue-coverage">No errors match this window.</p>}
        <p className="subtle issue-coverage">Stack and exception buckets can overlap. These locations show where errors were observed.</p>
      </Panel></div>
      <Panel icon={ListChecks} title="Most frequent issues" subtitle="Occurrences in the selected window" action={<Link className="text-link" to={`/issues?${params}`}>All issues</Link>}><IssueTable items={data.frequent_issues} /></Panel>
      <Panel icon={ChartNoAxesCombined} title="Fastest-growing issues" subtitle="Largest absolute increase against the previous period"><IssueTable items={data.growing_issues} /></Panel>
    </>}
  </>;
}
