// The manager is the only graph writer. MCP remains a read-only child process.
import { execFile, spawn } from 'node:child_process';
import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import { mkdir, readFile, readdir, realpath, rename, stat, writeFile } from 'node:fs/promises';
import http from 'node:http';
import { isAbsolute, join, relative, resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';
import { promisify } from 'node:util';

const execute = promisify(execFile);
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const VERSION = '1.6.12';
export class GraphError extends Error {}

async function atomicJson(path, value) {
  await writeFile(path + '.tmp', JSON.stringify(value), { mode: 0o600 });
  await rename(path + '.tmp', path);
}

export class GraphManager {
  constructor({ state = '/state', mount = '/repositories', cacheMount = process.env.IC_REPOSITORY_CACHE_DIR, tokenFile = '/run/codegraph-auth/token',
    token = process.env.GITNEXUS_MCP_AUTH_TOKEN, port = 3111, childPort = 3112,
    prepareTimeout = 300000, run = execute, spawnProcess = spawn } = {}) {
    Object.assign(this, { state, mount, cacheMount, tokenFile, token, port, childPort, prepareTimeout, run, spawnProcess });
    this.entries = new Map();
    this.jobs = new Map();
    this.queue = [];
    this.active = new Set();
    this.busy = false;
    this.serving = false;
    this.stopping = false;
  }

  async init() {
    this.mount = await realpath(this.mount);
    this.mounts = [this.mount];
    if (this.cacheMount) this.mounts.push(await realpath(this.cacheMount));
    await mkdir(join(this.state, 'repositories'), { recursive: true });
    await mkdir(join(this.state, 'home'), { recursive: true });
    await mkdir(resolve(this.tokenFile, '..'), { recursive: true });
    if (!this.token) {
      try { this.token = (await readFile(this.tokenFile, 'utf8')).trim(); } catch { /* First boot. */ }
    }
    this.token ||= randomBytes(32).toString('base64url');
    await writeFile(this.tokenFile + '.tmp', this.token, { mode: 0o600 });
    await rename(this.tokenFile + '.tmp', this.tokenFile);
    for (const key of await readdir(join(this.state, 'repositories'))) {
      if (!/^[a-f0-9]{32}$/.test(key)) continue;
      try {
        const entry = JSON.parse(await readFile(join(this.state, 'repositories', key, 'prepared.json'), 'utf8'));
        if (await this.canonical(entry.repository) !== entry.repository || this.key(entry.repository) !== key) continue;
        if (entry.provider_version !== VERSION || !(await stat(this.storage(entry.repository))).isDirectory()) continue;
        this.entries.set(entry.repository, entry);
      } catch { /* Interrupted/unverified generations are prepared again on demand. */ }
    }
  }

  key(root) { return createHash('sha256').update(root).digest('hex').slice(0, 32); }
  directory(root) { return join(this.state, 'repositories', this.key(root)); }
  storage(root) { return join(this.directory(root), 'index'); }

  async canonical(value) {
    if (typeof value !== 'string' || !isAbsolute(value) || value.length > 4096 || /[,\r\n\0]/.test(value)) throw new GraphError('invalid_repository');
    let root;
    try { root = await realpath(value); } catch { throw new GraphError('repository_unavailable'); }
    if (!(this.mounts || [this.mount]).some(mount => {
      const path = relative(mount, root);
      return path !== '..' && !path.startsWith('..' + sep) && !isAbsolute(path);
    })) throw new GraphError('repository_outside_mount');
    return root;
  }

  async revision(root) {
    try {
      const git = async (...args) => (await this.run('git', ['--no-optional-locks', '-c', `safe.directory=${root}`, '-C', root, ...args],
        { encoding: 'utf8', timeout: 10000, maxBuffer: 1048576 })).stdout.trim();
      if (await realpath(await git('rev-parse', '--show-toplevel')) !== root) throw new Error();
      const revision = await git('rev-parse', 'HEAD');
      if (await git('status', '--porcelain', '--untracked-files=normal', '--ignore-submodules=none')) throw new Error();
      if (await git('rev-parse', 'HEAD') !== revision) throw new Error();
      return revision;
    } catch { throw new GraphError('checkout_not_clean_or_unavailable'); }
  }

  async status(value) {
    const root = await this.canonical(value);
    const entry = this.entries.get(root);
    const job = this.jobs.get(root);
    let source_revision = null, reason = job?.reason || null;
    try { source_revision = await this.revision(root); } catch (error) { reason = error.message; }
    let status = 'not_prepared';
    if (job && ['queued', 'preparing'].includes(job.status)) status = job.status;
    else if (reason) status = 'unavailable';
    else if (entry) status = entry.revision === source_revision ? (this.serving ? 'ready' : 'queued') : 'stale';
    return { repository: root, status, source_revision, indexed_revision: entry?.revision || null,
      indexed_at: entry?.indexed_at || null, reason };
  }

  async ensure(value, expectedRevision = null, force = false) {
    if (this.stopping) throw new GraphError('manager_stopping');
    const root = await this.canonical(value);
    const revision = await this.revision(root);
    if (expectedRevision && expectedRevision !== revision) throw new GraphError('source_snapshot_mismatch');
    const existing = this.jobs.get(root);
    if (existing && ['queued', 'preparing'].includes(existing.status)) return this.status(root);
    if (!force && this.entries.get(root)?.revision === revision) {
      this.jobs.delete(root);
      if (!this.busy) await this.startMcp();
      return this.status(root);
    }
    this.jobs.set(root, { revision, status: 'queued', reason: null });
    this.queue.push(root);
    // The HTTP request returns before any indexing work starts.
    setImmediate(() => this.pump().catch(() => console.error('graph_manager_queue_failed')));
    return this.status(root);
  }

  environment(root) {
    const env = { ...process.env, HOME: join(this.state, 'home'), GITNEXUS_HOME: join(this.state, 'home', '.gitnexus'),
      GITNEXUS_CONTENT_RETENTION: 'none', GITNEXUS_SHARED_STORE: 'off' };
    delete env.GITNEXUS_STORAGE_ROOT;
    delete env.GITNEXUS_STORAGE_PATH;
    if (root) {
      env.GITNEXUS_STORAGE_PATH = this.storage(root);
      Object.assign(env, { GIT_CONFIG_COUNT: '1', GIT_CONFIG_KEY_0: 'safe.directory', GIT_CONFIG_VALUE_0: root });
    }
    return env;
  }

  async pump() {
    if (this.busy || this.stopping) return;
    this.busy = true;
    try {
      while (this.queue.length && !this.stopping) {
        const root = this.queue.shift(), job = this.jobs.get(root);
        job.status = 'preparing';
        try {
          if (await this.revision(root) !== job.revision) throw new GraphError('source_changed');
          await this.stopMcp();
          await mkdir(this.directory(root), { recursive: true });
          // Remove the verified marker before writing, even if analyze subsequently fails.
          this.entries.delete(root);
          await atomicJson(join(this.directory(root), 'prepared.json'), { repository: root, interrupted: true });
          await this.analyze(root);
          if (await this.revision(root) !== job.revision) throw new GraphError('source_changed');
          const entry = { repository: root, revision: job.revision, provider_version: VERSION, indexed_at: new Date().toISOString() };
          await atomicJson(join(this.directory(root), 'prepared.json'), entry);
          this.entries.set(root, entry);
          job.status = 'ready';
        } catch (error) {
          job.status = 'unavailable';
          job.reason = error instanceof GraphError ? error.message : 'preparation_failed';
          console.error(job.reason);
        }
      }
    } finally {
      this.busy = false;
      if (!this.stopping) await this.startMcp();
    }
  }

  async analyze(root) {
    // Force graph rebuilding: an interrupted generation may already have HEAD
    // metadata despite never passing our post-index source verification.
    const child = this.spawnProcess('gitnexus', ['analyze', root, '--force', '--name', `ic-${this.key(root)}`,
      '--index-only', '--drop-embeddings', '--skip-fts'],
      { env: this.environment(root), stdio: ['ignore', 'inherit', 'inherit'], detached: process.platform !== 'win32' });
    this.writer = child;
    try {
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => { reject(new GraphError('preparation_timeout')); this.signal(child, 'SIGKILL'); }, this.prepareTimeout);
        child.once('error', () => { clearTimeout(timer); reject(new GraphError('preparation_failed')); });
        child.once('exit', code => { clearTimeout(timer); code === 0 ? resolve() : reject(new GraphError('preparation_failed')); });
      });
    } finally { this.writer = null; }
  }

  signal(child, signal) {
    try { process.platform === 'win32' ? child.kill(signal) : process.kill(-child.pid, signal); } catch { /* Already exited. */ }
  }

  async stopMcp() {
    this.serving = false;
    const deadline = Date.now() + 15000;
    while (this.active.size && Date.now() < deadline) await delay(25);
    for (const response of this.active) response.destroy();
    const child = this.child;
    this.child = null;
    if (!child) return;
    await new Promise(resolve => {
      const finished = () => { clearTimeout(timer); resolve(); };
      const timer = setTimeout(() => { this.signal(child, 'SIGKILL'); finished(); }, 5000);
      child.once('exit', finished);
      child.once('error', finished);
      this.signal(child, 'SIGTERM');
    });
  }

  async startMcp() {
    if (this.stopping || this.busy || this.child || !this.entries.size) return;
    const env = { ...this.environment(), GITNEXUS_MCP_READ_ONLY: '1',
      GITNEXUS_MCP_ALLOWED_REPOS: [...this.entries.keys()].join(','), GITNEXUS_MCP_AUTH_TOKEN: this.token };
    const roots = [...this.entries.keys()];
    env.GIT_CONFIG_COUNT = String(roots.length);
    roots.forEach((root, index) => {
      env[`GIT_CONFIG_KEY_${index}`] = 'safe.directory';
      env[`GIT_CONFIG_VALUE_${index}`] = root;
    });
    const child = this.spawnProcess('gitnexus', ['mcp', '--http', '--host', '127.0.0.1', '--port', String(this.childPort)],
      { env, stdio: ['ignore', 'inherit', 'inherit'], detached: process.platform !== 'win32' });
    this.child = child;
    const lost = () => { if (this.child === child) { this.child = null; this.serving = false; } };
    child.once('error', lost);
    child.once('exit', lost);
    // Readiness is polled by the supervisor; management endpoints stay available.
  }

  async supervise() {
    if (this.busy || this.stopping) return;
    await this.startMcp();
    const child = this.child;
    if (child) {
      let healthy = false;
      try { healthy = (await fetch(`http://127.0.0.1:${this.childPort}/health`, { signal: AbortSignal.timeout(1000) })).ok; }
      catch { /* Native startup can take several supervisor ticks. */ }
      if (this.child !== child || this.busy || this.stopping) return;
      this.serving = healthy;
      this.unhealthyTicks = healthy ? 0 : (this.unhealthyTicks || 0) + 1;
      if (this.unhealthyTicks >= 30) {
        this.unhealthyTicks = 0;
        await this.stopMcp();
      }
    }
  }

  authorized(request) {
    const actual = Buffer.from(request.headers.authorization || ''), expected = Buffer.from(`Bearer ${this.token}`);
    return actual.length === expected.length && timingSafeEqual(actual, expected);
  }

  async handle(request, response) {
    const send = (code, body) => { response.writeHead(code, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(body)); };
    const url = new URL(request.url, 'http://localhost');
    if (request.method === 'GET' && url.pathname === '/health') return send(200, { status: 'ok' });
    if (!this.authorized(request)) return send(401, { reason: 'unauthorized' });
    if (url.pathname === '/mcp') {
      if (!this.serving || this.busy) return send(503, { reason: 'graph_preparing_or_unavailable' });
      this.active.add(response);
      response.once('close', () => this.active.delete(response));
      const upstream = http.request({ hostname: '127.0.0.1', port: this.childPort, path: '/mcp', method: request.method,
        headers: { ...request.headers, host: `127.0.0.1:${this.childPort}` } }, result => {
        response.writeHead(result.statusCode, result.headers);
        result.pipe(response);
      });
      upstream.on('error', () => { if (!response.headersSent) send(503, { reason: 'provider_unavailable' }); else response.destroy(); });
      response.once('close', () => upstream.destroy());
      request.pipe(upstream);
      return;
    }
    try {
      if (request.method === 'GET' && url.pathname === '/manage/repositories') return send(200, await this.status(url.searchParams.get('repository')));
      if (request.method === 'POST' && url.pathname === '/manage/ensure') {
        const chunks = []; let bytes = 0;
        for await (const chunk of request) {
          bytes += chunk.length;
          if (bytes > 8192) return send(413, { reason: 'request_too_large' });
          chunks.push(chunk);
        }
        const body = JSON.parse(Buffer.concat(chunks).toString());
        if (body.revision != null && (typeof body.revision !== 'string' || !/^[a-f0-9]{40,64}$/.test(body.revision))) throw new GraphError('invalid_revision');
        if (body.force != null && typeof body.force !== 'boolean') throw new GraphError('invalid_request');
        return send(202, await this.ensure(body.repository, body.revision, body.force === true));
      }
      return send(404, { reason: 'not_found' });
    } catch (error) { return send(422, { reason: error instanceof GraphError ? error.message : 'invalid_request' }); }
  }

  async listen() {
    await this.init();
    this.server = http.createServer((req, res) => { this.handle(req, res).catch(() => res.destroy()); });
    this.server.requestTimeout = 30000;
    await new Promise(resolve => this.server.listen(this.port, '0.0.0.0', resolve));
    this.timer = setInterval(() => this.supervise().catch(() => console.error('graph_supervisor_failed')), 2000);
  }

  async close() {
    this.stopping = true;
    clearInterval(this.timer);
    if (this.writer) this.signal(this.writer, 'SIGKILL');
    await this.stopMcp();
    if (this.server) {
      this.server.closeAllConnections();
      await new Promise(resolve => this.server.close(resolve));
    }
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const manager = new GraphManager();
  await manager.listen();
  for (const signal of ['SIGTERM', 'SIGINT']) process.once(signal, () => manager.close().then(() => process.exit(0)));
}
