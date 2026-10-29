import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, CircleHelp, FlaskConical, LockKeyhole, RotateCcw, Save, Settings2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { api, json } from '../api';
import { ErrorNotice, Loading, Notice, PageHeading } from '../components';
import type { SettingsData } from '../types';

type Field = { key: string; label: string; help?: string; type?: 'number' | 'password' | 'select'; options?: string[]; external?: boolean };
const groups: { title: string; description: string; fields: Field[] }[] = [
  { title: 'Language model', description: 'New analyses use LiteLLM Gateway by default. Deterministic diagnosis remains available if the model is unreachable.', fields: [
    { key: 'llm_provider', label: 'Provider', type: 'select', options: ['openai_compatible', 'ollama'] },
    { key: 'ollama_base_url', label: 'Ollama endpoint', help: 'Use a URL reachable from the worker.' }, { key: 'ollama_model', label: 'Ollama model' },
    { key: 'openai_compatible_base_url', label: 'OpenAI-compatible endpoint', help: 'The default LiteLLM endpoint is http://litellm:4000/v1 in Docker, or http://localhost:4000/v1 on the host.' },
    { key: 'openai_compatible_model', label: 'OpenAI-compatible model', help: 'For LiteLLM, use the gateway model alias, such as incident-diagnosis.' },
    { key: 'openai_compatible_api_key', label: 'API key', type: 'password', help: 'Stored server-side. Leave blank to keep the current key. For LiteLLM, use the gateway key.' },
  ] },
  { title: 'Analysis limits', description: 'Bound the investigation. Each run captures an immutable settings revision.', fields: [
    { key: 'top_k_candidates', label: 'Maximum candidates', type: 'number', help: 'Maximum source candidates retained by the diagnosis.' },
    { key: 'deep_analysis_top_n', label: 'Deep analysis candidates', type: 'number', help: 'Must not exceed the maximum candidates.' },
  ] },
  { title: 'Code graph', description: 'Prepared automatically for each repository. New analyses wait up to five minutes, then continue without graph evidence if needed.', fields: [
    { key: 'code_graph_enabled', label: 'Code graph evidence', type: 'select', options: ['false', 'true'] },
    { key: 'code_graph_mode', label: 'Graph service', type: 'select', options: ['managed', 'external'], help: 'Managed starts with the application and follows the selected repository.' },
    { key: 'code_graph_prepare_timeout_seconds', label: 'Preparation deadline (seconds)', type: 'number', help: 'Maximum 300 seconds, separate from graph retrieval.' },
    { key: 'gitnexus_url', label: 'GitNexus MCP endpoint', external: true, help: 'Use a URL reachable from the worker, ending in /mcp.' },
    { key: 'gitnexus_repository', label: 'Indexed repository path', external: true, help: 'Optional server-side path. Blank uses the incident repository path.' },
    { key: 'gitnexus_auth_token', label: 'GitNexus token', external: true, type: 'password', help: 'Stored server-side. Leave blank to keep the current token.' },
    { key: 'code_graph_timeout_seconds', label: 'Graph deadline (seconds)', type: 'number', help: 'Maximum 15 seconds for all retrieval calls.' },
  ] },
  { title: 'Evidence investigation', description: 'Optional iterative diagnosis for new analyses. The model can request source evidence within these limits.', fields: [
    { key: 'agentic_enabled', label: 'Iterative evidence retrieval', type: 'select', options: ['false', 'true'] },
    { key: 'agentic_max_rounds', label: 'Maximum retrieval rounds', type: 'number', help: 'Up to 3 rounds of source investigation.' },
    { key: 'agentic_max_tool_requests', label: 'Maximum evidence requests', type: 'number', help: 'Up to 12 requests across the investigation.' },
    { key: 'agentic_timeout_seconds', label: 'Investigation deadline (seconds)', type: 'number', help: 'Up to 180 seconds, including worker retries.' },
  ] },
  { title: 'Guardrails', description: 'Optional prompt injection and credential disclosure checks. Start in observation mode and review findings before enforcing. Applies to new analyses.', fields: [
    { key: 'guardrails_mode', label: 'Guardrails mode', type: 'select', options: ['off', 'observe', 'enforce'] },
    { key: 'guardrails_timeout_seconds', label: 'Policy check timeout (seconds)', type: 'number', help: 'Maximum time per check, within the investigation deadline.' },
  ] },
  { title: 'Temporal', description: 'Saved changes take effect after the API and worker restart, once active incidents finish.', fields: [
    { key: 'temporal_host', label: 'Temporal address', help: 'For example localhost:7233 or temporal:7233 inside Docker.' },
    { key: 'temporal_namespace', label: 'Namespace' }, { key: 'temporal_task_queue', label: 'Task queue' },
    { key: 'temporal_connect_max_attempts', label: 'Connection attempts', type: 'number' },
    { key: 'temporal_connect_retry_delay_seconds', label: 'Retry delay (seconds)', type: 'number' },
  ] },
  { title: 'Application & storage', description: 'Startup settings. Historical artifacts stay at their original paths when storage changes.', fields: [
    { key: 'repository_cache_dir', label: 'Repository cache directory', help: 'Worker-owned GitHub snapshots. Mount this directory at the same path in the API, worker, and graph service; changes require restart.' },
    { key: 'app_name', label: 'Application name' }, { key: 'env', label: 'Environment' },
    { key: 'data_dir', label: 'Data directory' }, { key: 'index_db_path', label: 'Index base path', help: 'Each dashboard run creates an isolated database under this path’s parent directory.' },
  ] },
];

export function Settings() {
  const query = useQuery({ queryKey: ['settings'], queryFn: () => api<SettingsData>('/settings') });
  const client = useQueryClient();
  const [values, setValues] = useState<Record<string, string | number | boolean>>({});
  const [resets, setResets] = useState<string[]>([]);
  const [message, setMessage] = useState('');
  const [checkResult, setCheckResult] = useState<{ ok: boolean; message: string } | null>(null);
  useEffect(() => { if (query.data) { setValues(query.data.saved); setResets([]); } }, [query.data]);
  const changed = Object.fromEntries(Object.entries(values).filter(([key, value]) => !resets.includes(key) && value !== query.data?.saved[key]));
  const dirty = !!Object.keys(changed).length || !!resets.length;
  const save = useMutation({ mutationFn: () => api<SettingsData>('/settings', json('PUT', { values: changed, reset: resets })), onSuccess: data => { client.setQueryData(['settings'], data); setValues(data.saved); setResets([]); setMessage('Configuration saved. Analysis and model settings apply to new runs.'); } });
  const check = useMutation({ mutationFn: (target: string) => api<{ ok: boolean; message: string }>('/connections/check', json('POST', { target })), onSuccess: setCheckResult });
  return <><PageHeading eyebrow="WORKSPACE SETTINGS" title="Configuration" description="Tune the investigation engine and connect your local infrastructure." action={<button form="settings-form" className="button primary" disabled={!dirty || save.isPending}><Save size={16} />{save.isPending ? 'Saving…' : 'Save changes'}</button>} />
    <ErrorNotice error={query.error || save.error || check.error} />{message && <Notice>{message}</Notice>}{query.data?.restart_required.length ? <Notice>Restart required for: {query.data.restart_required.map(key => key.replaceAll('_', ' ')).join(', ')}. Finish active incidents, then restart both the API and worker. Saved and active values are shown below.</Notice> : null}
    {query.isPending ? <Loading /> : query.data && <div className="settings-layout"><form id="settings-form" className="settings-form" onSubmit={e => { e.preventDefault(); save.mutate(); }}>{groups.map((group, index) => {
      const restart = group.fields.some(field => query.data!.infrastructure_fields.includes(field.key));
      const renderField = (field: Field) => {
      const pending = query.data!.restart_required.includes(field.key);
      return <div className="setting-field" key={field.key}><div className="setting-label"><label htmlFor={field.key}>{field.label}</label>{query.data!.overridden.includes(field.key) && <span className="override-label">OVERRIDE</span>}<button type="button" className={`reset-button ${resets.includes(field.key) ? 'reset-pending' : ''}`} aria-label={`Reset ${field.label} to environment default`} title="Reset to environment or built-in default on save" onClick={() => setResets(old => old.includes(field.key) ? old.filter(k => k !== field.key) : [...old, field.key])}><RotateCcw size={12} />{resets.includes(field.key) ? 'Reset pending' : 'Reset'}</button></div>{field.type === 'select' ? <select id={field.key} value={String(values[field.key] ?? '')} disabled={resets.includes(field.key)} onChange={e => setValues(old => ({ ...old, [field.key]: ['code_graph_enabled', 'agentic_enabled'].includes(field.key) ? e.target.value === 'true' : e.target.value }))}>{field.options?.map(option => <option key={option} value={option}>{option === 'managed' ? 'Managed automatically' : option === 'external' ? 'External (advanced)' : option === 'ollama' ? 'Ollama' : option === 'openai_compatible' ? 'OpenAI compatible' : option === 'true' ? 'Enabled' : option === 'false' ? 'Disabled' : option === 'off' ? 'Off' : option === 'observe' ? 'Observe' : option === 'enforce' ? 'Enforce' : option}</option>)}</select> : <input id={field.key} type={field.type || 'text'} autoComplete={field.type === 'password' ? 'new-password' : 'off'} min={field.type === 'number' ? 1 : undefined} max={({ code_graph_timeout_seconds: 15, code_graph_prepare_timeout_seconds: 300, agentic_max_rounds: 3, agentic_max_tool_requests: 12, agentic_timeout_seconds: 180, guardrails_timeout_seconds: 180 } as Record<string, number>)[field.key]} required={field.type !== 'password' && field.key !== 'gitnexus_repository' && field.key !== 'repository_cache_dir' && (group.title !== 'Code graph' || values.code_graph_enabled === true) && (group.title !== 'Evidence investigation' || values.agentic_enabled === true) && (group.title !== 'Guardrails' || ['observe', 'enforce'].includes(String(values.guardrails_mode))) && !resets.includes(field.key)} value={String(values[field.key] ?? '')} disabled={resets.includes(field.key)} placeholder={field.type === 'password' && (field.key === 'gitnexus_auth_token' ? query.data!.graph_secret_configured : query.data!.secret_configured) ? '•••••••• · key configured' : undefined} onChange={e => setValues(old => ({ ...old, [field.key]: field.type === 'number' ? (e.target.value === '' ? '' : Number(e.target.value)) : e.target.value }))} />}{field.help && <small>{field.help}</small>}{pending && <small className="active-value">Active: <code>{String(query.data!.active[field.key])}</code></small>}</div>;
      };
      return <section className="panel settings-group" id={`settings-${index}`} key={group.title}><div className="panel-heading"><div><h2>{group.title}</h2><p>{group.description}</p></div><span className={`settings-mode ${restart ? '' : 'hot'}`}>{restart ? 'Restart required' : 'Next analysis'}</span></div><div className="settings-fields">{group.fields.filter(field => !field.external).map(renderField)}</div>{group.title === 'Code graph' && values.code_graph_mode === 'external' && <details className="graph-advanced"><summary>Advanced external-service settings</summary><div className="settings-fields">{group.fields.filter(field => field.external).map(renderField)}</div></details>}</section>;
    })}<div className="form-actions settings-save"><span>{dirty ? 'You have unsaved changes.' : 'Configuration is up to date.'}</span><button className="button primary" disabled={!dirty || save.isPending}><Save size={15} />Save changes</button></div></form>
    <aside className="settings-aside"><div className="panel connection-checks"><h2><FlaskConical size={17} />Connection checks</h2><p>Test the active saved configuration. Save any edits before checking.</p>{['temporal', 'llm', 'storage'].map(target => <button key={target} className="button" disabled={check.isPending} onClick={() => { setCheckResult(null); check.mutate(target); }}><span>{target === 'llm' ? 'Language model' : target[0].toUpperCase() + target.slice(1)}</span>{check.isPending && check.variables === target ? 'Checking…' : 'Test connection'}</button>)}{checkResult && <div className={`check-result ${checkResult.ok ? 'ok' : 'bad'}`} role="status">{checkResult.message}</div>}</div><div className="panel settings-guide"><LockKeyhole size={21} className="lime" /><h3>Server-side credentials</h3><p>API keys stay in the local control store. They are excluded from configuration responses and Temporal payloads.</p><hr /><h3>Settings precedence</h3><p>Saved dashboard overrides take priority over environment values. Reset a field to return to its environment or built-in default.</p><hr /><h3>Control store</h3><p className="mono control-path">{query.data.control_db_path}</p><small>Bootstrap location · set with IC_CONTROL_DB_PATH before starting the API and worker.</small></div></aside></div>}
  </>;
}
