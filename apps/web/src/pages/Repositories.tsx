import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Archive, CheckCircle2, Circle, FileCode2, FolderGit2, GitBranch, Github, HardDrive, LoaderCircle, Pencil, Plus, Save, Server, ShieldCheck, TriangleAlert, X } from 'lucide-react';
import { useRef, useState } from 'react';
import { api, json } from '../api';
import { ErrorNotice, Loading, PageHeading } from '../components';
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
  const formTrigger = useRef<HTMLButtonElement | null>(null);
  const profileInput = useRef<HTMLInputElement>(null);
  const closeEditor = () => {
    setEditing(null); setToken('');
    requestAnimationFrame(() => formTrigger.current?.focus());
  };
  const refresh = () => client.invalidateQueries({ queryKey: ['repositories'] });
  const save = useMutation({ mutationFn: () => api<Repository>(editing === 'new' ? '/repositories' : `/repositories/${editing}`, json(editing === 'new' ? 'POST' : 'PUT', {
    name: name.trim(), source_type: source, compose_path: compose.trim() || null,
    ...(source === 'local' ? { repo_root: root.trim() } : { remote_url: remote.trim(), branch: branch.trim(), github_token: token || undefined, clear_github_token: clearToken }),
  })), onSuccess: repo => {
    refresh(); client.removeQueries({ queryKey: ['repository-sync', repo.id] });
    client.invalidateQueries({ queryKey: ['repository-graph', repo.id] });
    closeEditor();
    setMessage(repo.source_type === 'github' ? 'Repository profile saved. Use Check & sync to download and index the tracked branch.' : 'Repository profile saved. Validate it to check access from the worker.');
  } });
  const archive = useMutation({ mutationFn: (id: string) => api(`/repositories/${id}/archive`, json('POST')), onSuccess: () => { refresh(); setMessage('Repository archived. Existing incident history is preserved.'); } });
  const validate = useMutation({ mutationFn: (id: string) => api<Validation>(`/repositories/${id}/validate`, json('POST')), onMutate: id => setChecking(id), onSuccess: result => { refresh(); setMessage(result.message); }, onSettled: () => setChecking(null) });
  const edit = (trigger: HTMLButtonElement, repo?: Repository) => {
    formTrigger.current = trigger;
    setEditing(repo?.id || 'new'); setName(repo?.name || ''); setSource(repo?.source_type || 'local');
    setRoot(repo?.repo_root || ''); setRemote(repo?.remote_url || ''); setBranch(repo?.branch || '');
    setToken(''); setClearToken(false); setTokenConfigured(!!repo?.token_configured);
    setCompose(repo?.compose_path || ''); save.reset();
    requestAnimationFrame(() => profileInput.current?.focus());
  };
  return <div className="connections-page"><PageHeading eyebrow="SOURCE CONNECTIONS" title="Repositories" description="Manage source connections, sync status, and code graph readiness." action={<button className="button primary" disabled={save.isPending} onClick={event => edit(event.currentTarget)}><Plus size={16} />Add repository</button>} />
    <ErrorNotice error={query.error || archive.error || validate.error} />{message && <div className="notice connection-success" role="status"><CheckCircle2 size={18} /><span>{message}</span></div>}
    {editing && <form className="panel repository-form" aria-labelledby="repository-form-title" aria-busy={save.isPending} onSubmit={e => { e.preventDefault(); if (!save.isPending) save.mutate(); }}>
      <div className="connection-form-heading"><div className="connection-icon"><FolderGit2 size={21} /></div><div><h2 id="repository-form-title">{editing === 'new' ? 'Connect a repository' : 'Edit repository profile'}</h2><p>Give your investigations the source context they need.</p></div><button type="button" className="icon-button" aria-label="Close repository form" disabled={save.isPending} onClick={closeEditor}><X size={18} /></button></div>
      <fieldset className="connection-form-body" disabled={save.isPending}>
      <fieldset className="connection-form-section"><legend>Profile</legend><div className="connection-fields">
      <label>Profile name<input ref={profileInput} autoFocus required maxLength={120} value={name} onChange={e => setName(e.target.value)} placeholder="e.g. Commerce platform" /></label>
      <label>Repository source<select value={source} onChange={e => setSource(e.target.value as 'local' | 'github')}><option value="local">Local directory</option><option value="github">GitHub</option></select></label>
      </div></fieldset>
      <fieldset className="connection-form-section"><legend>{source === 'github' ? 'GitHub connection' : 'Worker connection'}</legend><div className="connection-fields">
      {source === 'local' ? <label className="connection-field-wide">Absolute repository path on worker<input required value={root} onChange={e => setRoot(e.target.value)} placeholder="e.g. /repositories/commerce or C:\Dev\commerce" className="mono" /></label> : <>
        <label className="connection-field-wide">GitHub repository URL<input required type="url" value={remote} onChange={e => setRemote(e.target.value)} placeholder="https://github.com/owner/repository" /></label>
        <label>Tracked branch<input required value={branch} onChange={e => setBranch(e.target.value)} placeholder="e.g. main" className="mono" /></label>
        <label>GitHub token <span className="optional">optional for public repositories</span><input type="password" autoComplete="new-password" value={token} disabled={clearToken} onChange={e => setToken(e.target.value)} placeholder={tokenConfigured ? 'Token saved; leave blank to keep it' : 'Fine-grained token with Contents: read access'} /></label>
        {tokenConfigured && <label className="issue-checkbox connection-field-wide"><input type="checkbox" checked={clearToken} onChange={e => setClearToken(e.target.checked)} /> Remove saved GitHub token</label>}
        <p className="connection-field-wide">Private repositories need a token with read access to the selected repository. Saving does not contact GitHub.</p>
      </>}
      </div></fieldset>
      <fieldset className="connection-form-section"><legend>Analysis configuration</legend>
      <label>Compose file <span className="optional">{source === 'github' ? 'optional, relative to repository' : 'optional, relative to repository or absolute'}</span><input value={compose} onChange={e => setCompose(e.target.value)} placeholder="docker-compose.yml" className="mono" /></label>
      </fieldset>
      <ErrorNotice error={save.error} />
      <div className="form-actions"><span>{source === 'local' ? 'Paths are validated on the analysis worker.' : 'Updates are downloaded only when you click Check & sync.'}</span><div className="button-group"><button type="button" className="button" onClick={closeEditor}>Cancel</button><button className="button primary" disabled={save.isPending || !name.trim() || (source === 'local' ? !root.trim() : !remote.trim() || !branch.trim())}>{save.isPending ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}{save.isPending ? 'Saving…' : 'Save profile'}</button></div></div>
      </fieldset>
    </form>}
    <div className="repository-info"><ShieldCheck size={19} /><p>Local paths refer to the <strong>worker’s filesystem</strong>. For Docker, mount your source directory and use its container path. GitHub repositories are downloaded into a managed cache using an outbound connection.</p></div>
    {query.isPending ? <Loading /> : query.isError && !query.data ? null : !query.data?.length ? <section className="connection-empty"><div className="connection-icon"><FolderGit2 size={26} /></div><h2>Connect your first repository</h2><p>Choose a local directory or a GitHub repository to give incident logs source-level context.</p></section> : <div className="repository-grid">{query.data.map(repo => <article className={`repository-card${editing === repo.id ? ' is-configuring' : ''}`} key={repo.id} aria-label={`Repository ${repo.name}`}>
      <div className="repo-top"><div className="connection-type"><div className={`connection-icon ${repo.source_type === 'github' ? 'github' : ''}`}>{repo.source_type === 'github' ? <Github size={21} /> : <HardDrive size={21} />}</div><span>{repo.source_type === 'github' ? 'GitHub' : 'Local directory'}</span></div><span className={`connection-status ${repo.validation ? repo.validation.valid ? 'is-success' : 'is-warning' : 'is-neutral'}`}>{repo.validation ? repo.validation.valid ? <CheckCircle2 size={13} /> : <TriangleAlert size={13} /> : <Circle size={12} />}{repo.validation ? repo.validation.valid ? 'Validated' : 'Needs attention' : 'Not validated'}</span></div>
      <h2>{repo.name}</h2><p className="repo-path mono">{repo.source_type === 'github' ? repo.remote_url : repo.repo_root}</p>
      <dl className="connection-metadata">
      {repo.source_type === 'github' && <div><dt><GitBranch size={14} />Branch</dt><dd className="mono">{repo.branch || 'Unknown'}</dd></div>}
      <div><dt><FileCode2 size={14} />Compose</dt><dd className="mono">{repo.compose_path || 'Not configured'}</dd></div>
      </dl>
      <div className="repo-stats"><div><strong>{repo.index_summary?.indexed_files ?? '—'}</strong><small>Indexed files</small></div><div><strong>{repo.index_summary?.indexed_symbols ?? '—'}</strong><small>Symbols</small></div><div><strong>{repo.validation?.services.length ?? '—'}</strong><small>Services</small></div></div>
      {repo.validation && <p className={`validation-message ${repo.validation.valid ? '' : 'invalid'}`}>{repo.validation.valid ? <CheckCircle2 size={15} /> : <TriangleAlert size={15} />}<span>{repo.validation.message}</span></p>}
      {!!repo.validation?.services.length && <div className="service-chips">{repo.validation.services.map(s => <span key={s.service_name}><Server size={11} />{s.service_name}</span>)}</div>}
      {repo.source_type === 'github' && <RepositorySync repo={repo} />}
      {(repo.source_type !== 'github' || repo.synced_revision) && <RepositoryGraph repo={repo} />}
      <div className="repo-actions">{repo.source_type !== 'github' && <button className="button" disabled={!!checking} onClick={() => validate.mutate(repo.id)}>{checking === repo.id ? <LoaderCircle className="spin" size={14} /> : <CheckCircle2 size={14} />}{checking === repo.id ? 'Validating…' : 'Validate access'}</button>}<div className="connection-secondary-actions"><button className="icon-button" title={`Edit ${repo.name}`} aria-label={`Edit ${repo.name}`} disabled={save.isPending} onClick={event => edit(event.currentTarget, repo)}><Pencil size={15} /></button><button className="icon-button connection-archive" title={`Archive ${repo.name}`} aria-label={`Archive ${repo.name}`} disabled={archive.isPending} onClick={() => archive.mutate(repo.id)}>{archive.isPending && archive.variables === repo.id ? <LoaderCircle className="spin" size={15} /> : <Archive size={15} />}</button></div></div>
    </article>)}</div>}
  </div>;
}
