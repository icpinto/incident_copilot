# Optional code graph evidence

GitNexus 1.6.12 supplies bounded static callers/callees. SQLite localization and candidate scores remain unchanged. The feature is disabled by default. A graph outage, stale index, dirty checkout, unresolved symbol, malformed response, or missing Python extra preserves the normal diagnosis path and records a reason in `graph_context`.

## Start the research service

Use a **clean, committed Git checkout**. Set `IC_REPOSITORIES_DIR` to the host directory mounted at `/repositories`, as with the existing dashboard. Select `/repositories` as the repository profile when this is one repository. To select a nested checkout, set `IC_GITNEXUS_REPOSITORY` to its absolute container path, such as `/repositories/payments`, and use that profile path too. One code-graph service stores one selected repository.

The service has its own Node 22.18.0 Debian image; the Python worker only needs Git and the MCP client. No host Node upgrade is needed. The source mount stays read-only. Indexes, registry, locks, and the preparation marker live in the `gitnexus-state` named volume. Embeddings, full-text indexing, content retention, agent files, and skills are disabled for this prototype.

PowerShell, from the project root (keep the same token across API/worker/service restarts):

```powershell
$env:IC_GITNEXUS_AUTH_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
docker compose -f docker-compose.dev.yml --profile code-graph build gitnexus
docker compose -f docker-compose.dev.yml --profile code-graph stop gitnexus
docker compose -f docker-compose.dev.yml --profile code-graph run --rm --no-deps gitnexus prepare
docker compose -f docker-compose.dev.yml --profile code-graph up -d gitnexus
$env:IC_CODE_GRAPH_ENABLED = 'true'
docker compose -f docker-compose.dev.yml up -d --build api worker
```

For persistent configuration, keep the token and feature flag in your private `.env`. Saved dashboard overrides take precedence: use **Configuration → Code graph** to enable evidence and, if overridden, update the token. Tokens are masked in configuration responses and redacted from incident/report exports, including historical configured values. Finish old incidents before changing their service's repository selection.

Preparation requires a clean checkout including untracked files, and verifies HEAD again after indexing. `prepare` holds an exclusive lock; `serve` holds a shared lock. **Stop the service before refreshing**, otherwise preparation waits for it. Repeat the stop/prepare/start sequence after source commits. Failed preparation removes its readiness marker; the server cannot serve that unverified generation. A stale server can remain running, but its evidence is skipped until refreshed. Running incidents retain their saved graph evidence when reports are generated or exported later.

For host Python processes, install `pip install -e ".[codegraph]"`, use `IC_GITNEXUS_URL=http://localhost:3111/mcp`, and set `IC_GITNEXUS_REPOSITORY` to the indexed container path. The host checkout must have the same clean commit. Docker applications use `http://gitnexus:3111/mcp`. HTTP binds beyond loopback require the shared token; the published host port is bound to localhost.

## Evidence contract

`graph_context` is optional in incident evidence and JSON/Markdown reports. Existing reports without it still load. It includes provider/version, selected repository, source and indexed revisions, index timestamp, symbols with source ranges, directed `CALLS` relationships, per-seed outcomes, retrieval duration, and status/completeness.

Retrieval runs in the analysis stage, before hypotheses, through the same provider helper for dashboard, synchronous API, CLI, and Temporal activities. At most three localized candidates and twenty related symbols are used, within one fifteen-second deadline. Names alone never resolve duplicates: file and symbol ranges must match the local incident index. Related source locations are checked against that index too. Both graph generation and clean checkout revision are checked before/after retrieval. MCP JSON is parsed separately from GitNexus hints; truncation and provider instructions are never passed through.

`available` means the bounded retrieval succeeded; `partial` records limits or lower-bound analysis; `skipped` records missing/unsafe localization or freshness; `unavailable` records dependency/transport/deadline failures. Even an exact provider result remains `bounded`, since GitNexus caps reference windows. Static paths do not prove runtime execution or root cause. Dynamic dispatch, reflection, and framework boundaries can leave calls unresolved. Graph evidence adds no candidate scores and triggers no source edits, raw Cypher, renames, embeddings, or model-controlled tool loop.

## Compare results

Ten labeled source cases live under `tests/fixtures/code_graph`: Python and TypeScript direct calls, wrappers, entry points, duplicate method names, and dynamic dispatch. Copy one language fixture to a separate directory, initialize and commit it as a Git repository, mount and prepare that repository as above. The opt-in real-provider suite expects precisely these fixture contents:

```powershell
$env:IC_RUN_GITNEXUS_TESTS = '1'
$env:IC_GRAPH_TEST_REPO_ROOT = 'C:\path\to\committed-python-fixture'
$env:IC_GRAPH_TEST_LANGUAGE = 'python'
$env:IC_GITNEXUS_REPOSITORY = '/repositories'
python -m pytest tests/test_gitnexus_integration.py -q
```

For an existing incident, prepare its clean revision and run a paired comparison:

```powershell
python -m incident_copilot.evaluation.benchmark_runner --repo-root C:\path\to\repo --log-file C:\path\to\error.log --actual-file services/payments.py --output data/benchmarks/comparison.json
```

Or use `--golden-case python-wrapper` in place of `--log-file` for a prepared fixture. The runner indexes/ranks once, uses the same model/settings and source evidence twice, and varies graph evidence only. Outputs include both reports, unchanged-ranking assertion, top-five hit, labeled edge coverage when applicable, retrieval latency, Python allocation peak, and UTF-8 prompt size/token estimate. Python allocation peaks exclude native/server memory. Measure real sidecar memory separately with `docker stats --no-stream`; the report leaves this field empty. Unsupported claims and hypothesis quality require human review, and are left unscored. Check `engine` and `fallback_reason`: two deterministic fallbacks do not establish an LLM quality improvement.

Normal checks are `python -m pytest -q`, `npm test` and `npm run build` under `apps/web`. The regular suite uses controlled providers and the real MCP SDK over JSON/SSE transports; it does not require a Docker daemon or external model. The opt-in suite exercises the actual prepared GitNexus graph and is skipped by default.

## Research licensing

GitNexus declares [PolyForm Noncommercial 1.0.0](https://github.com/abhigyanpatwari/GitNexus/blob/v1.6.12/LICENSE). Reassess licensing before company/product adoption. This integration is a personal research prototype; it is not an accuracy benchmark result.