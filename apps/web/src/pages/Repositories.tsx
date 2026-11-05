import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Archive, CheckCircle2, FolderGit2, GitBranch, Pencil, Plus, Save, Server, ShieldCheck, X } from 'lucide-react';
import { useState } from 'react';
import { api, json } from '../api';
import { Empty, ErrorNotice, Loading, Notice, PageHeading } from '../components';
import type { Repository, Validation } from '../types';
import { RepositoryGraph } from '../RepositoryGraph';
import { RepositorySync } from '../RepositorySync';

export function Repositories() {
  const query = useQuery({ queryKey: ['repositories'], queryFn: () => api<Repository[]>('/repositories') });
  const client = useQueryClient();
  const [editing, setEditing] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [source, setSource] = useState<'local' | 'github'>('local');
  const [root, setRoot] = useState('');
  const [remote, setRemote] = useState('');
  const [branch, setBranch] = useState('');
  const [token, setToken] = useState('');
  const [clearToken, setClearToken] = useState(false);
  const [tokenConfigured, setTokenConfigured] = useState(false);
  const [compose, setCompose] = useState('');
  const [message, setMessage] = useState('');
  const [checking, setChecking] = useState<string | null>(null);
  const refresh = () => client.invalidateQueries({ queryKey: ['repositories'] });
  const save = useMutation({ mutationFn: () => api<Repository>(editing === 'new' ? '/repositories' : `/repositories/${editing}`, json(editing === 'new' ? 'POST' : 'PUT', {
    name: name.trim(), source_type: source, compose_path: compose.trim() || null,
    ...(source === 'local' ? { repo_root: root.trim() } : { remote_url: remote.trim(), branch: branch.trim(), github_token: token || undefined, clear_github_token: clearToken }),
  })), onSuccess: repo => {
    refresh(); client.removeQueries({ queryKey: ['repository-sync', repo.id] });
    client.invalidateQueries({ queryKey: ['repository-graph', repo.id] });
    setEditing(null); setToken('');
    setMessage(repo.source_type === 'github' ? 'Repository profile saved. Use Check & sync to download and index the tracked branch.' : 'Repository profile saved. Validate it to check access from the worker.');
  } });
  const archive = useMutation({ mutationFn: (id: string) => api(`/repositories/${id}/archive`, json('POST')), onSuccess: () => { refresh(); setMessage('Repository archived. Existing incident history is preserved.'); } });
  const validate = useMutation({ mutationFn: (id: string) => api<Validation>(`/repositories/${id}/validate`, json('POST')), onMutate: id => setChecking(id), onSuccess: result => { refresh(); setMessage(result.message); }, onSettled: () => setChecking(null) });
  const edit = (repo?: Repository) => {
    setEditing(repo?.id || 'new'); setName(repo?.name || ''); setSource(repo?.source_type || 'local');
    setRoot(repo?.repo_root || ''); setRemote(repo?.remote_url || ''); setBranch(repo?.branch || '');
    setToken(''); setClearToken(false); setTokenConfigured(!!repo?.token_configured);
    setCompose(repo?.compose_path || ''); save.reset();
  };
  return <><PageHeading eyebrow="SOURCE CONNECTIONS" title="Repositories" description="Manage source connections, sync status, and code graph readiness." action={<button className="button primary" onClick={() => edit()}><Plus size={16} />Add repository</button>} />
    <ErrorNotice error={query.error || save.error || archive.error || validate.error} />{message && <Notice>{message}</Notice>}
    {editing && <form className="panel repository-form" onSubmit={e => { e.preventDefault(); save.mutate(); }}>
      <div className="inline-heading"><h2>{editing === 'new' ? 'Connect a repository' : 'Edit repository profile'}</h2><button type="button" className="icon-button" aria-label="Close repository form" onClick={() => { setEditing(null); setToken(''); }}><X size={18} /></button></div>
      <label>Profile name<input required maxLength={120} value={name} onChange={e => setName(e.target.value)} placeholder="e.g. Commerce platform" /></label>
      <label>Repository source<select value={source} onChange={e => setSource(e.target.value as 'local' | 'github')}><option value="local">Local directory</option><option value="github">GitHub</option></select></label>
      {source === 'local' ? <label>Absolute repository path on worker<input required value={root} onChange={e => setRoot(e.target.value)} placeholder="e.g. /repositories/commerce or C:\Dev\commerce" className="mono" /></label> : <>
        <label>GitHub repository URL<input required type="url" value={remote} onChange={e => setRemote(e.target.value)} placeholder="https://github.com/owner/repository" /></label>
        <label>Tracked branch<input required value={branch} onChange={e => setBranch(e.target.value)} placeholder="e.g. main" className="mono" /></label>
        <label>GitHub token <span className="optional">optional for public repositories</span><input type="password" autoComplete="new-password" value={token} disabled={clearToken} onChange={e => setToken(e.target.value)} placeholder={tokenConfigured ? 'Token saved; leave blank to keep it' : 'Fine-grained token with Contents: read access'} /></label>
        {tokenConfigured && <label><span><input type="checkbox" checked={clearToken} onChange={e => setClearToken(e.target.checked)} /> Remove saved GitHub token</span></label>}
        <p>Private repositories need a token with read access to the selected repository. Saving does not contact GitHub.</p>
      </>}
      <label>Compose file <span className="optional">{source === 'github' ? 'optional, relative to repository' : 'optional, relative to repository or absolute'}</span><input value={compose} onChange={e => setCompose(e.target.value)} placeholder="docker-compose.yml" className="mono" /></label>
      <div className="form-actions"><span>{source === 'local' ? 'Paths are validated on the analysis worker.' : 'Updates are downloaded only when you click Check & sync.'}</span><button className="button primary" disabled={save.isPending || !name.trim() || (source === 'local' ? !root.trim() : !remote.trim() || !branch.trim())}><Save size={15} />{save.isPending ? 'Saving…' : 'Save profile'}</button></div>
    </form>}
    <div className="repository-info"><ShieldCheck size={19} /><p>Local paths refer to the <strong>worker’s filesystem</strong>. For Docker, mount your source directory and use its container path. GitHub repositories are downloaded into a managed cache using an outbound connection.</p></div>
    {query.isPending ? <Loading /> : !query.data?.length ? <Empty title="Connect your first repository" description="Choose a local directory or a GitHub repository to give incident logs source-level context." action={false} /> : <div className="repository-grid">{query.data.map(repo => <article className="repository-card" key={repo.id}>
      <div className="repo-top"><div className="repo-icon"><FolderGit2 size={23} strokeWidth={1.5} /></div><span className={`repo-status ${repo.validation?.valid ? 'valid' : ''}`}><span className="status-dot" />{repo.source_type === 'github' ? 'GitHub' : 'Local directory'} · {repo.validation ? repo.validation.valid ? 'Validated' : 'Needs attention' : 'Not validated'}</span></div>
      <h2>{repo.name}</h2><p className="repo-path mono">{repo.source_type === 'github' ? repo.remote_url : repo.repo_root}</p>
      {repo.source_type === 'github' && <div className="repo-compose"><GitBranch size={14} /><span>{repo.branch}</span></div>}
      <div className="repo-compose"><GitBranch size={14} /><span>{repo.compose_path || 'No Compose file configured'}</span></div>
      <div className="repo-stats"><div><strong>{repo.index_summary?.indexed_files ?? '—'}</strong><small>Indexed files</small></div><div><strong>{repo.index_summary?.indexed_symbols ?? '—'}</strong><small>Symbols</small></div><div><strong>{repo.validation?.services.length ?? '—'}</strong><small>Services</small></div></div>
      {repo.validation && <p className={`validation-message ${repo.validation.valid ? '' : 'invalid'}`}>{repo.validation.message}</p>}
      {!!repo.validation?.services.length && <div className="service-chips">{repo.validation.services.map(s => <span key={s.service_name}><Server size={11} />{s.service_name}</span>)}</div>}
      {repo.source_type === 'github' && <RepositorySync repo={repo} />}
      {(repo.source_type !== 'github' || repo.synced_revision) && <RepositoryGraph repo={repo} />}
      <div className="repo-actions">{repo.source_type !== 'github' && <button className="button" disabled={!!checking} onClick={() => validate.mutate(repo.id)}><CheckCircle2 size={14} />{checking === repo.id ? 'Validating…' : 'Validate access'}</button>}<button className="icon-button" aria-label={`Edit ${repo.name}`} onClick={() => edit(repo)}><Pencil size={15} /></button><button className="icon-button" aria-label={`Archive ${repo.name}`} disabled={archive.isPending} onClick={() => archive.mutate(repo.id)}><Archive size={15} /></button></div>
    </article>)}</div>}
  </>;
}
