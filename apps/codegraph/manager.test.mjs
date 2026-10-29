import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { mkdir, mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import http from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { promisify } from 'node:util';
import { GraphManager } from './manager.mjs';

const execute = promisify(execFile);
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(check) {
  for (let attempt = 0; attempt < 400; attempt++) {
    if (await check()) return;
    await delay(25);
  }
  assert.fail('Timed out waiting for graph lifecycle');
}

async function fixture(t) {
  const base = await mkdtemp(join(tmpdir(), 'codegraph-'));
  t.after(() => rm(base, { recursive: true, force: true }));
  const mount = join(base, 'sources'); await mkdir(mount);
  const manager = new GraphManager({ state: join(base, 'state'), mount, tokenFile: join(base, 'auth', 'token') });
  await manager.init();
  manager.startMcp = async () => { manager.serving = manager.entries.size > 0; };
  manager.stopMcp = async () => { manager.serving = false; };
  const analyzed = [];
  manager.analyze = async root => { analyzed.push(root); await mkdir(manager.storage(root), { recursive: true }); };
  async function repo(name = 'repo') {
    const root = join(mount, name); await mkdir(root, { recursive: true });
    const git = (...args) => execute('git', ['-C', root, ...args]);
    await git('init');
    await writeFile(join(root, 'main.py'), 'def main(): pass\n');
    await git('add', '.');
    await git('-c', 'user.name=Graph Test', '-c', 'user.email=graph@example.invalid', 'commit', '-m', 'source');
    return { root: await manager.canonical(root), git };
  }
  return { base, manager, repo, analyzed };
}

test('first use, revision reuse, refresh, and multiple repositories remain isolated', async t => {
  const { manager, repo, analyzed } = await fixture(t);
  const one = await repo('group-one/app'), two = await repo('group-two/app');
  await manager.ensure(one.root);
  await until(() => !manager.busy && manager.jobs.get(one.root)?.status === 'ready');
  const first = await manager.status(one.root);
  assert.equal(first.status, 'ready');
  await manager.ensure(one.root);
  assert.equal(analyzed.length, 1);
  await writeFile(join(one.root, 'main.py'), 'def main(): return 1\n');
  await one.git('add', '.');
  await one.git('-c', 'user.name=Graph Test', '-c', 'user.email=graph@example.invalid', 'commit', '-m', 'update');
  assert.equal((await manager.status(one.root)).status, 'stale');
  await Promise.all([manager.ensure(one.root), manager.ensure(two.root)]);
  await until(() => !manager.busy && manager.entries.size === 2);
  assert.equal(analyzed.length, 3);
  assert.notEqual(manager.storage(one.root), manager.storage(two.root));
  assert.notEqual((await manager.status(one.root)).indexed_revision, first.indexed_revision);
  assert.equal((await manager.status(two.root)).status, 'ready');
});

test('deduplicates requests, serializes writers, and retries a failed preparation', async t => {
  const { manager, repo } = await fixture(t);
  const one = await repo('one'), two = await repo('two');
  let active = 0, peak = 0, calls = 0, failed = false;
  manager.analyze = async root => {
    calls++; peak = Math.max(peak, ++active);
    await delay(100);
    active--;
    if (root === one.root && !failed) { failed = true; throw new Error('private provider text'); }
    await mkdir(manager.storage(root), { recursive: true });
  };
  await Promise.all([manager.ensure(one.root), manager.ensure(one.root), manager.ensure(two.root)]);
  await until(() => !manager.busy && manager.jobs.get(two.root)?.status === 'ready');
  assert.equal(calls, 2); assert.equal(peak, 1);
  assert.equal((await manager.status(one.root)).reason, 'preparation_failed');
  await manager.ensure(one.root);
  await until(() => !manager.busy && manager.jobs.get(one.root)?.status === 'ready');
  assert.equal(calls, 3);
});

test('permits managed cache worktrees alongside local sources and rejects escaping links', async t => {
  const { base, manager, repo } = await fixture(t);
  const local = await repo();
  const cache = join(base, 'cache'); await mkdir(cache);
  const cached = join(cache, 'snapshot');
  await local.git('worktree', 'add', '--detach', cached, 'HEAD');
  await assert.rejects(manager.canonical(cached), /repository_outside_mount/);
  manager.cacheMount = cache;
  await manager.init();
  assert.equal(await manager.revision(await manager.canonical(cached)), await manager.revision(local.root));
  await manager.ensure(cached);
  await until(() => !manager.busy && manager.entries.size === 1);
  assert.equal((await manager.status(cached)).status, 'ready');
  const escape = join(cache, 'escape'); await symlink(base, escape, 'junction');
  await assert.rejects(manager.canonical(escape), /repository_outside_mount/);
});

test('rejects dirty, missing, nested, escaping, and mismatched checkouts', async t => {
  const { base, manager, repo } = await fixture(t);
  const { root } = await repo();
  await assert.rejects(manager.ensure(root, 'a'.repeat(40)), /source_snapshot_mismatch/);
  await writeFile(join(root, 'untracked.py'), 'pass');
  await assert.rejects(manager.ensure(root), /checkout_not_clean_or_unavailable/);
  await assert.rejects(manager.ensure(join(root, 'missing')), /repository_unavailable/);
  await mkdir(join(root, 'nested'));
  await assert.rejects(manager.ensure(join(root, 'nested')), /checkout_not_clean_or_unavailable/);
  await assert.rejects(manager.ensure(base), /repository_outside_mount/);
  const escape = join(manager.mount, 'escape');
  await symlink(base, escape, 'junction');
  await assert.rejects(manager.ensure(escape), /repository_outside_mount/);
});

test('does not publish an index when source changes during preparation', async t => {
  const { manager, repo } = await fixture(t);
  const { root } = await repo();
  manager.analyze = async () => { await writeFile(join(root, 'main.py'), 'changed'); };
  await manager.ensure(root);
  await until(() => !manager.busy && manager.jobs.get(root)?.status === 'unavailable');
  assert.equal(manager.entries.size, 0);
  assert.equal((await manager.status(root)).status, 'unavailable');
});

test('retains tokens and verified indexes after restart, discards interrupted markers', async t => {
  const { manager, repo } = await fixture(t);
  const { root } = await repo();
  await manager.ensure(root);
  await until(() => !manager.busy && manager.entries.size === 1);
  const restarted = new GraphManager({ state: manager.state, mount: manager.mount, tokenFile: manager.tokenFile });
  await restarted.init();
  assert.equal(restarted.token, manager.token);
  assert.equal(restarted.entries.size, 1);
  await writeFile(join(manager.directory(root), 'prepared.json'), JSON.stringify({ repository: root, interrupted: true }));
  const interrupted = new GraphManager({ state: manager.state, mount: manager.mount, tokenFile: manager.tokenFile, token: 'configured-token' });
  await interrupted.init();
  assert.equal(interrupted.entries.size, 0);
  assert.equal(await readFile(manager.tokenFile, 'utf8'), 'configured-token');
});

test('health is independent of indexes; management and MCP require authentication', async t => {
  const { manager, repo } = await fixture(t);
  const { root } = await repo();
  const server = http.createServer((req, res) => manager.handle(req, res));
  server.listen(0, '127.0.0.1'); await new Promise(resolve => server.once('listening', resolve));
  t.after(() => { server.closeAllConnections(); server.close(); });
  const url = `http://127.0.0.1:${server.address().port}`;
  assert.equal((await fetch(url + '/health')).status, 200);
  assert.equal((await fetch(url + '/manage/repositories?repository=' + encodeURIComponent(root))).status, 401);
  const headers = { Authorization: `Bearer ${manager.token}` };
  const state = await fetch(url + '/manage/repositories?repository=' + encodeURIComponent(root), { headers });
  assert.equal((await state.json()).status, 'not_prepared');
  assert.equal((await fetch(url + '/mcp', { headers })).status, 503);
});

test('MCP proxy preserves sessions, drains reads before terminating the child', async t => {
  const { manager } = await fixture(t);
  const childServer = http.createServer((req, res) => {
    assert.equal(req.headers['mcp-session-id'], 'session');
    setTimeout(() => { res.writeHead(200, { 'mcp-session-id': 'session' }); res.end('{"ok":true}'); }, 100);
  });
  childServer.listen(0, '127.0.0.1'); await new Promise(resolve => childServer.once('listening', resolve));
  manager.childPort = childServer.address().port;
  const server = http.createServer((req, res) => manager.handle(req, res));
  server.listen(0, '127.0.0.1'); await new Promise(resolve => server.once('listening', resolve));
  t.after(() => { childServer.closeAllConnections(); childServer.close(); server.closeAllConnections(); server.close(); });
  manager.serving = true;
  const child = new EventEmitter(); manager.child = child;
  let drained = false;
  manager.signal = () => { drained = manager.active.size === 0; child.emit('exit', 0); };
  const response = fetch(`http://127.0.0.1:${server.address().port}/mcp`, { method: 'POST', body: '{}',
    headers: { Authorization: `Bearer ${manager.token}`, 'mcp-session-id': 'session' } });
  await until(() => manager.active.size === 1);
  // Use the real drain implementation, overriding fixture's stub.
  const stopping = GraphManager.prototype.stopMcp.call(manager);
  assert.equal((await response).headers.get('mcp-session-id'), 'session');
  await stopping;
  assert.equal(drained, true); assert.equal(manager.child, null);
});

test('analyzer deadline kills the process; child exits are recovered by supervision', async t => {
  const { manager } = await fixture(t);
  manager.prepareTimeout = 10;
  let killed = false;
  manager.spawnProcess = (_command, args, options) => {
    assert.ok(args.includes('--force'));
    assert.equal(args[args.indexOf('--name') + 1], `ic-${manager.key(manager.mount)}`);
    assert.equal(options.env.GITNEXUS_STORAGE_PATH, manager.storage(manager.mount));
    return new EventEmitter();
  };
  manager.signal = child => { killed = true; child.emit('exit', 1); };
  await assert.rejects(GraphManager.prototype.analyze.call(manager, manager.mount), /preparation/);
  assert.equal(killed, true);
  manager.entries.set(manager.mount, {});
  manager.entries.set(join(manager.mount, 'second'), {});
  let env;
  manager.spawnProcess = (_command, _args, options) => { env = options.env; return new EventEmitter(); };
  await GraphManager.prototype.startMcp.call(manager);
  assert.equal(env.GIT_CONFIG_COUNT, '2');
  assert.equal(env.GIT_CONFIG_VALUE_0, manager.mount);
  assert.equal(env.GIT_CONFIG_VALUE_1, join(manager.mount, 'second'));
  assert.equal(env.GITNEXUS_MCP_READ_ONLY, '1');
  const first = manager.child; first.emit('exit', 1);
  assert.equal(manager.child, null);
  await GraphManager.prototype.startMcp.call(manager);
  assert.notEqual(manager.child, first);
});
