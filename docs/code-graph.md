# Managed code graph evidence

GitNexus 1.6.12 supplies bounded static callers/callees. Graph evidence is enabled by default and prepared automatically for each incident's selected repository. SQLite localization and candidate scores remain unchanged. Graph failures preserve normal diagnosis and record a reason in `graph_context`.

## Start and use the application

With Docker Desktop running, start every service from the project root:

```powershell
docker compose -f docker-compose.dev.yml up --build -d
```

Open **http://localhost:8080**, add a repository profile, and start an analysis. No Compose profile, manual token, or preparation command is required. The analysis stage shows graph preparation progress and waits up to five minutes before continuing without graph evidence. Retrieval has a separate fifteen-second deadline. If another preparation occupies the service, that queue time counts toward the analysis's preparation deadline. A timed-out or canceled analysis does not cancel a shared preparation job; a later analysis can reuse it.

Use a **clean, committed Git checkout**, including no untracked source files. The worker and graph service share the read-only `/repositories` mount. Set `IC_REPOSITORIES_DIR` before startup to choose the host directory mounted there. Use `/repositories` for one checkout, or `/repositories/payments`, `/repositories/catalog`, etc. when mounting a parent directory containing several checkouts. Every profile must select an exact checkout root. Paths outside that mount, including escaping symlinks, are rejected.

**Repositories** shows readiness, indexed/current commits, preparation time, and a **Prepare graph / Refresh graph** action. Analyses automatically reuse an index at the current clean commit and refresh it after source commits. Status checks never start indexing. A dirty checkout, changing source, stale graph, missing symbol, or graph outage leaves normal diagnosis available.

**Configuration > Code graph** controls graph evidence, managed/external mode, preparation budget (at most 300 seconds), and retrieval budget (at most 15 seconds). Changes apply to new analyses without restarting API or worker. Explicit saved or environment disables remain respected; reset a saved override to use the new enabled default.

For a normal stack restart:

```powershell
docker compose -f docker-compose.dev.yml restart
```

The manager owns an internal read-only MCP child and handles stop/prepare/start around graph writes. It deduplicates requests, serializes writers, drains active MCP HTTP requests, and restarts the MCP child after preparation. Reads during refresh can fall back safely; existing incidents keep their saved graph evidence. A generation change during iterative investigation is rejected rather than mixed with earlier evidence.

## Persistence and recovery

The `gitnexus-state` volume stores independent indexes, the registry, and verified preparation markers beneath `/state/repositories`. Legacy `/state/index` data is retained; managed mode rebuilds an index on first use. A failed or interrupted write has no verified marker and is prepared again on demand. Verified indexes are reused after restart. The manager remains healthy before indexing any repository and restarts a failed MCP child; graph readiness does not gate the main application's startup.

A separate `gitnexus-auth` volume stores an automatically generated token, mounted read-only into API and worker. An explicit `IC_GITNEXUS_AUTH_TOKEN` is preserved when supplied at startup. Managed clients read the shared token file (`IC_GITNEXUS_AUTH_TOKEN_FILE`) rather than stale dashboard credentials. Tokens never appear in settings responses, progress, or reports. The published port remains bound to localhost; management and MCP endpoints require authentication. The application does not need access to the Docker socket.

The graph service uses its own Node 22.18.0 image. Source mounts stay read-only; embeddings, full-text indexing, source content retention, agent files, and skills remain disabled.

## External services and host Python

For host Python processes with a separately prepared GitNexus service, install `pip install -e ".[codegraph]"`, select **External (advanced)** under Code graph, and expand the external-service settings. Set `IC_CODE_GRAPH_MODE=external`, `IC_GITNEXUS_URL=http://localhost:3111/mcp`, and the service's token and indexed repository path. The host checkout must match the provider's clean commit. External mode performs retrieval only; it never requests preparation or refresh. For a custom endpoint inside Compose, use `IC_GITNEXUS_DOCKER_URL` or a saved dashboard endpoint override.

The typed dashboard endpoints are `GET /api/repositories/{id}/graph` and `POST /api/repositories/{id}/graph/refresh` (202). Status includes readiness, source/index revisions, indexing time, and a fixed reason code. The browser communicates only with the main API.

## Evidence contract

`graph_context` is optional in incident evidence and JSON/Markdown reports. Existing reports without it still load. It includes provider/version, selected repository, source and indexed revisions, index timestamp, symbols with source ranges, directed `CALLS` relationships, per-seed outcomes, retrieval duration, and status/completeness.

Retrieval runs in the analysis stage, before hypotheses, through the same provider helper for dashboard, synchronous API, CLI, and Temporal activities. At most three localized candidates and twenty related symbols are used, within one fifteen-second deadline. Names alone never resolve duplicates: file and symbol ranges must match the local incident index. Related source locations are checked against that index too. Both graph generation and clean checkout revision are checked before/after retrieval. MCP JSON is parsed separately from GitNexus hints; truncation and provider instructions are never passed through.

`available` means the bounded retrieval succeeded; `partial` records limits or lower-bound analysis; `skipped` records missing/unsafe localization or freshness; `unavailable` records dependency/transport/deadline failures. Even an exact provider result remains `bounded`, since GitNexus caps reference windows. Static paths do not prove runtime execution or root cause. Dynamic dispatch, reflection, and framework boundaries can leave calls unresolved. Graph evidence adds no candidate scores and triggers no source edits, raw Cypher, renames, embeddings, or model-controlled tool loop.

## Compare results

Ten labeled source cases live under `tests/fixtures/code_graph`: Python and TypeScript direct calls, wrappers, entry points, duplicate method names, and dynamic dispatch. Copy one language fixture to a separate directory, initialize and commit it as a Git repository, mount that repository and use its dashboard Prepare graph action. The opt-in real-provider suite expects precisely these fixture contents:

```powershell
$env:IC_RUN_GITNEXUS_TESTS = '1'
$env:IC_CODE_GRAPH_MODE = 'external'
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

Normal checks are `python -m pytest -q`, `node --test apps/codegraph/manager.test.mjs` (Node 22), and `npm test` / `npm run build` under `apps/web`. The regular suite uses controlled providers and the real MCP SDK over JSON/SSE transports; it does not require a Docker daemon or external model. The opt-in suite exercises the actual prepared GitNexus graph and is skipped by default.

## Research licensing

GitNexus declares [PolyForm Noncommercial 1.0.0](https://github.com/abhigyanpatwari/GitNexus/blob/v1.6.12/LICENSE). Reassess licensing before company/product adoption. This integration is a personal research prototype; it is not an accuracy benchmark result.