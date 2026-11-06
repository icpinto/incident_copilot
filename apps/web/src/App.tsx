import { useQuery } from '@tanstack/react-query';
import { Activity, ArrowUpRight, ChevronRight, FolderGit2, LayoutDashboard, Radio, Search, Settings2, Terminal } from 'lucide-react';
import { Link, NavLink, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { useEffect, useState } from 'react';
import { Overview } from './pages/Overview';
import { Incidents, NewIncident, IncidentDetail } from './pages/Incidents';
import { Repositories } from './pages/Repositories';
import { Settings } from './pages/Settings';
import { Empty } from './components';
import { ErrorIntelligence } from './pages/ErrorIntelligence';
import { Issues, IssueDetail } from './pages/Issues';
import { LogSources } from './pages/LogSources';

export function App() {
  const location = useLocation();
  const navigate = useNavigate();
  const [search, setSearch] = useState('');
  const health = useQuery({
    queryKey: ['health'],
    queryFn: async () => {
      const response = await fetch('/health');
      if (!response.ok) throw new Error('API unavailable');
      const data = await response.json();
      if (data.status !== 'ok') throw new Error('API unavailable');
      return data;
    },
    refetchInterval: 15000,
    retry: false,
  });
  const section = location.pathname.startsWith('/intelligence') ? 'Error intelligence'
    : location.pathname.startsWith('/log-sources') ? 'Log sources'
    : location.pathname.startsWith('/issues') ? 'Issues'
    : location.pathname.startsWith('/repositories') ? 'Repositories'
    : location.pathname.startsWith('/settings') ? 'Configuration'
    : location.pathname.startsWith('/incidents') ? 'Incidents' : 'Overview';
  const detail = location.pathname === '/incidents/new' ? 'New analysis'
    : location.pathname.startsWith('/incidents/') ? 'Investigation' : null;
  useEffect(() => {
    setSearch(new URLSearchParams(location.search).get('q') || '');
  }, [location.search, location.pathname]);

  return <div className="app-shell">
    <a className="skip-link" href="#main">Skip to content</a>
    <aside className="sidebar">
      <Link to="/" className="brand" aria-label="Incident Copilot home">
        <span className="brand-mark"><Activity size={22} strokeWidth={2} /></span>
        <span>incident<span className="brand-secondary">copilot</span><small>OPERATIONS CONSOLE</small></span>
      </Link>
      <div className="workspace-picker">
        <div className="workspace-icon"><Terminal size={17} /></div>
        <div><strong>Local workspace</strong><small>Incident investigation</small></div>
      </div>
      <div className="nav-section-label">WORKSPACE</div>
      <nav aria-label="Main navigation">
        <NavLink to="/" end><LayoutDashboard size={18} />Overview</NavLink>
        <NavLink to="/intelligence"><LayoutDashboard size={18} />Error intelligence</NavLink>
        <NavLink to="/issues"><Search size={18} />Issues</NavLink>
        <NavLink to="/incidents"><Activity size={18} />Incidents</NavLink>
        <NavLink to="/repositories"><FolderGit2 size={18} />Repositories</NavLink>
        <NavLink to="/log-sources"><Radio size={18} />Log sources</NavLink>
        <NavLink to="/settings"><Settings2 size={18} />Configuration</NavLink>
      </nav>
      <div className="sidebar-bottom">
        <Link className="sidebar-shortcut" to="/incidents/new">New analysis<ArrowUpRight size={16} /></Link>
        <div className="local-mode"><Terminal size={14} /><span>Local environment</span><span className="mono">v0.1</span></div>
      </div>
    </aside>
    <div className="workspace">
      <header className="topbar">
        <nav className="breadcrumb" aria-label="Breadcrumb">
          <span>Workspace</span><ChevronRight size={13} />
          {detail ? <><Link to="/incidents">{section}</Link><ChevronRight size={13} /><strong aria-current="page">{detail}</strong></> : <strong aria-current="page">{section}</strong>}
        </nav>
        <form className="global-search" role="search" onSubmit={e => { e.preventDefault(); const q = search.trim(); navigate(`/incidents${q ? `?q=${encodeURIComponent(q)}` : ''}`); }}>
          <Search size={16} /><input aria-label="Search incidents" placeholder="Search incidents…" value={search} onChange={e => setSearch(e.target.value)} />
          <button type="submit" className="search-submit" aria-label="Submit incident search"><ArrowUpRight size={15} /></button>
        </form>
        <span className={`connection-pill ${health.isError ? 'offline' : health.isPending ? 'connecting' : 'connected'}`} role="status" title={health.isError ? 'Start the local API to reconnect' : health.isPending ? 'Checking the local API' : 'Local API is reachable'}>
          <Radio size={14} />{health.isError ? 'API offline' : health.isPending ? 'Connecting' : 'API connected'}
        </span>
        <Link to="/settings" className="avatar" aria-label="Workspace configuration"><Settings2 size={17} /></Link>
      </header>
      <main id="main" tabIndex={-1}>
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/intelligence" element={<ErrorIntelligence />} />
          <Route path="/issues" element={<Issues />} />
          <Route path="/issues/:id" element={<IssueDetail />} />
          <Route path="/incidents" element={<Incidents />} />
          <Route path="/incidents/new" element={<NewIncident />} />
          <Route path="/incidents/:id" element={<IncidentDetail />} />
          <Route path="/repositories" element={<Repositories />} />
          <Route path="/log-sources" element={<LogSources />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Empty title="Page not found" description="Use the workspace navigation to return to your incidents." action={false} />} />
        </Routes>
        <footer className="page-footer"><span><Activity size={14} />Incident Copilot</span><span>Evidence-led incident diagnosis</span></footer>
      </main>
    </div>
  </div>;
}
