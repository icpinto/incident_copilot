import { useQuery } from '@tanstack/react-query';
import { Activity, ArrowRight, ArrowUpRight, ChartNoAxesCombined, CheckCheck, Clock3, FolderGit2, ListChecks, Plus, Zap } from 'lucide-react';
import { Area, AreaChart, Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { Link } from 'react-router-dom';
import { api } from '../api';
import { chartTheme, chartTooltip } from '../chartTheme';
import { duration, Empty, ErrorNotice, IncidentTable, Loading, PageHeading, Panel } from '../components';
import type { OverviewData, Repository } from '../types';

export function Overview() {
  const query = useQuery({ queryKey: ['overview'], queryFn: () => api<OverviewData>('/overview'), refetchInterval: q => q.state.data?.active ? 2000 : false });
  const repos = useQuery({ queryKey: ['repositories'], queryFn: () => api<Repository[]>('/repositories') });
  const data = query.data;
  return <>
    <PageHeading eyebrow="WORKSPACE / OVERVIEW" title="Incident overview" description="Monitor investigations and follow the evidence." action={<Link className="button primary" to="/incidents/new"><Plus size={17} />New analysis</Link>} />
    <ErrorNotice error={query.error} />
    <ErrorNotice error={repos.error} />
    {query.isPending ? <Loading /> : data && <>
      <div className="overview-status">
        <span><span className={`status-dot ${data.active ? 'lime' : ''}`} />{data.active ? `${data.active} investigation${data.active === 1 ? '' : 's'} in progress` : data.total ? 'No investigations in progress' : 'Ready for your first investigation'}</span>
        {!data.total && <Link className="text-link" to={repos.data?.length ? '/incidents/new' : '/repositories'}>{repos.data?.length ? 'Start an analysis' : 'Connect a repository'}<ArrowRight size={14} /></Link>}
        <span className="overview-scope">Saved incident history</span>
      </div>
      <div className="metrics-grid">{[
        { label: 'Total incidents', value: data.total, note: 'Across all repositories', icon: Activity },
        { label: 'Completed analyses', value: data.completed, note: data.total ? `${Math.round(data.completed / data.total * 100)}% of submitted incidents` : 'No completed analyses yet', icon: CheckCheck },
        { label: 'Active investigations', value: data.active, note: `${data.failed} failed ${data.failed === 1 ? 'run' : 'runs'} in history`, icon: Zap },
        { label: 'Avg. diagnosis time', value: data.completed ? duration(data.average_duration_seconds) : '—', note: 'Submission to completed report', icon: Clock3 },
      ].map(metric => <section className="metric" key={metric.label} aria-label={metric.label}>
        <div className="metric-top"><span>{metric.label}</span><span className="metric-icon"><metric.icon size={17} /></span></div>
        <strong>{metric.value}</strong><small>{metric.note}</small>
      </section>)}</div>
      <div className="charts-grid">
        <Panel icon={ChartNoAxesCombined} title="Incident activity" subtitle="Submitted incidents over the last 14 days" action={<span className="chart-period">Last 14 days</span>}>
          <div className="chart" role="img" aria-label={`Incident submissions over 14 days; ${data.total} total incidents`}>
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={data.activity} margin={{ top: 16, right: 24, left: -12, bottom: 0 }}>
                <defs><linearGradient id="activity-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor={chartTheme.accent} stopOpacity={0.18} /><stop offset="100%" stopColor={chartTheme.accent} stopOpacity={0} /></linearGradient></defs>
                <CartesianGrid stroke={chartTheme.grid} vertical={false} strokeDasharray="3 5" />
                <XAxis dataKey="date" tickFormatter={v => new Date(`${v}T00:00:00`).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} tick={chartTheme.tick} axisLine={false} tickLine={false} minTickGap={35} tickMargin={12} />
                <YAxis allowDecimals={false} tick={chartTheme.tick} axisLine={false} tickLine={false} />
                <Tooltip contentStyle={chartTooltip} />
                <Area isAnimationActive={false} type="monotone" dataKey="incidents" name="Incidents" stroke={chartTheme.accent} fill="url(#activity-fill)" strokeWidth={2} activeDot={{ r: 4, fill: chartTheme.accent, stroke: 'var(--panel)', strokeWidth: 2 }} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
          <div className="chart-footnote"><span className="chart-legend"><i />Incident volume</span><span>Daily submissions</span></div>
        </Panel>
        <Panel icon={FolderGit2} title="Service distribution" subtitle="Likely service in completed diagnoses">
          {data.services.length ? <div className="chart" role="img" aria-label={data.services.map(s => `${s.name}: ${s.count}`).join(', ')}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data.services.slice(0, 6)} layout="vertical" margin={{ top: 16, right: 24, left: 0 }}>
                <XAxis type="number" hide allowDecimals={false} /><YAxis type="category" dataKey="name" width={120} tick={chartTheme.tick} axisLine={false} tickLine={false} />
                <Tooltip contentStyle={chartTooltip} cursor={chartTheme.cursor} /><Bar isAnimationActive={false} dataKey="count" name="Incidents" fill={chartTheme.secondary} radius={[0, 4, 4, 0]} barSize={12} />
              </BarChart>
            </ResponsiveContainer>
          </div> : <div className="chart-empty"><FolderGit2 size={25} strokeWidth={1.5} /><strong>No service data yet</strong><p>Completed diagnoses will appear here.</p></div>}
          <div className="chart-footnote"><span>{data.services.length} services identified</span><span>Diagnosis results</span></div>
        </Panel>
      </div>
      <Panel icon={ListChecks} title="Recent investigations" subtitle="Latest incidents and their current status" action={<Link className="text-link" to="/incidents">View all incidents<ArrowUpRight size={15} /></Link>}>
        {data.recent.length ? <IncidentTable rows={data.recent} /> : <Empty />}
      </Panel>
      <div className="workflow-note"><span>Index <ArrowRight size={12} />Correlate <ArrowRight size={12} />Analyze <ArrowRight size={12} />Diagnose</span><span><FolderGit2 size={14} />{repos.data ? `${repos.data.length} connected repositories` : 'Repository count unavailable'}</span></div>
    </>}
  </>;
}
