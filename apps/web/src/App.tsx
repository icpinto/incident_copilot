import { useQuery } from '@tanstack/react-query';
import { Activity, ArrowUpRight, BookOpen, ChevronRight, CircleHelp, Command, FolderGit2, LayoutDashboard, Plus, Radio, Search, Settings2, Terminal, Workflow } from 'lucide-react';
import { Link, NavLink, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { useState } from 'react';
import { api } from './api';
import { Overview } from './pages/Overview';
import { Incidents, NewIncident, IncidentDetail } from './pages/Incidents';
import { Repositories } from './pages/Repositories';
import { Settings } from './pages/Settings';
import { Empty } from './components';

export function App() {
  const location = useLocation();
  const navigate = useNavigate();
  const [search, setSearch] = useState('');
  const health = useQuery({ queryKey: ['health'], queryFn: async () => {
    const response = await fetch('/health'); if (!response.ok) throw new Error('API unavailable'); return response.json();
  }, refetchInterval: 15000, retry: false });
  const title = location.pathname.startsWith('/repositories') ? 'Repositories' : location.pathname.startsWith('/settings') ? 'Configuration' : location.pathname.startsWith('/incidents') ? 'Incidents' : 'Overview';
  return <div className="app-shell"><a className="skip-link" href="#main">Skip to content</a>
    <aside className="sidebar"><Link to="/" className="brand"><span className="brand-mark"><Activity size={24} strokeWidth={2.2} /></span><span>incident<span className="brand-secondary">copilot</span><small>OPERATIONS WORKSPACE</small></span></Link>
      <div className="workspace-picker"><div className="workspace-icon"><Terminal size={17} /></div><div><strong>Local workspace</strong><small>Diagnosis first. Evidence always.</small></div><span className="tiny-dot" /></div>
      <div className="nav-section-label">WORKSPACE</div><nav aria-label="Main navigation"><NavLink to="/" end><LayoutDashboard size={18} />Overview</NavLink><NavLink to="/incidents"><Activity size={18} />Incidents</NavLink><NavLink to="/repositories"><FolderGit2 size={18} />Repositories</NavLink><NavLink to="/settings"><Settings2 size={18} />Configuration</NavLink></nav>
      <div className="sidebar-bottom"><div className="sidebar-note"><Workflow size={20} /><strong>Built for the investigation</strong><p>From the first error to the next right check. Keep every signal in view.</p><Link to="/incidents/new">Start an analysis <ArrowUpRight size={14} /></Link></div><div className="local-mode"><span className="status-dot" /><span>Local environment</span><span className="mono">v0.1</span></div></div>
    </aside>
    <div className="workspace"><header className="topbar"><div className="breadcrumb"><Command size={16} /><span>Workspace</span><ChevronRight size={13} /><strong>{title}</strong></div><form className="global-search" role="search" onSubmit={e => { e.preventDefault(); navigate(`/incidents?q=${encodeURIComponent(search)}`); }}><Search size={16} /><input aria-label="Search incidents" placeholder="Search incidents…" value={search} onChange={e => setSearch(e.target.value)} /><kbd>↵</kbd></form><span className={`connection-pill ${health.isError ? 'offline' : ''}`} title={health.isError ? 'Start the local API to reconnect' : 'Local API is reachable'}><Radio size={13} />{health.isError ? 'API offline' : health.isPending ? 'Connecting' : 'API connected'}</span><Link to="/settings" className="avatar" aria-label="Workspace configuration">IC</Link></header>
    <main id="main" tabIndex={-1}><Routes><Route path="/" element={<Overview />} /><Route path="/incidents" element={<Incidents />} /><Route path="/incidents/new" element={<NewIncident />} /><Route path="/incidents/:id" element={<IncidentDetail />} /><Route path="/repositories" element={<Repositories />} /><Route path="/settings" element={<Settings />} /><Route path="*" element={<Empty title="Page not found" description="Use the workspace navigation to return to your incidents." action={false} />} /></Routes><footer className="page-footer"><span><Activity size={13} /> Incident Copilot</span><span>Local analysis · Durable workflows · Evidence-led diagnosis</span></footer></main></div>
  </div>;
}
