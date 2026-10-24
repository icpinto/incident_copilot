import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

const root = resolve(process.env.GITNEXUS_REPO_ROOT || '/repositories');
const marker = '/state/prepared.json';
const version = '1.6.12';
function git(...args) {
  return execFileSync('git', ['--no-optional-locks', '-C', root, ...args], { encoding: 'utf8', timeout: 10000 }).trim();
}
function cleanRevision() {
  if (resolve(git('rev-parse', '--show-toplevel')) !== root) throw new Error('Select an exact Git checkout root.');
  const revision = git('rev-parse', 'HEAD');
  if (git('status', '--porcelain', '--untracked-files=normal', '--ignore-submodules=none')) {
    throw new Error('Graph preparation requires a clean checkout, including untracked files.');
  }
  if (git('rev-parse', 'HEAD') !== revision) throw new Error('Checkout changed during validation.');
  return revision;
}
try {
  if (process.argv.includes('--check')) {
    const prepared = JSON.parse(readFileSync(marker, 'utf8'));
    if (prepared.repository !== root || prepared.provider_version !== version || !existsSync('/state/index')) {
      throw new Error('Prepare the selected repository with this GitNexus version first.');
    }
  } else {
    const revision = cleanRevision();
    rmSync(marker, { force: true });
    execFileSync('gitnexus', ['analyze', root, '--index-only', '--skip-embeddings', '--skip-fts'], { stdio: 'inherit' });
    if (cleanRevision() !== revision) throw new Error('Checkout changed during indexing. Prepare it again.');
    writeFileSync(marker + '.tmp', JSON.stringify({ repository: root, revision, provider_version: version }));
    renameSync(marker + '.tmp', marker);
    console.log(`Prepared ${root} at ${revision} with GitNexus ${version}.`);
  }
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
