# Incident Copilot

A diagnosis-first incident copilot for containerized monorepos.

This version intentionally **does not patch code**. It does:

1. Ingest user-provided error logs
2. Parse a local or manually synced GitHub monorepo and docker-compose metadata
3. Correlate logs to likely services/files/functions
4. Run optional deeper AST analysis on top candidates
5. Use a bounded LangGraph reasoning step to produce a ranked diagnosis report
6. Orchestrate the flow with Temporal

## Why this shape

The system is split into two clear layers:

- **Temporal**: durable outer workflow, retries, activity boundaries
- **LangGraph**: bounded diagnosis reasoning over curated evidence

The diagnosis pipeline is:

1. Service map extraction
2. Fast repo indexing
3. Log correlation and candidate ranking
4. Conditional deep analysis
5. LLM-assisted hypothesis ranking
6. Markdown / JSON diagnosis report

Optional [structured causal reasoning and selective review](docs/structured-reasoning.md)
apply to both diagnosis modes. Baseline remains the default until paired human evaluation
supports promotion.

Reports separate raw localization strength from diagnosis confidence and allow an
explicit insufficient-evidence outcome. Diagnosis probabilities remain unavailable
pending labeled calibration data. See [confidence semantics and human review](docs/diagnosis-confidence.md).

New reports provide [actionable recommendations](docs/actionable-recommendations.md): cited evidence,
evidence gaps, distinguishing checks, repository runbooks, mitigation prerequisites and
recovery verification. The dashboard records manual responder outcomes for handoffs.

New analyses build an [incident timeline](docs/incident-timeline.md) before ranking.
Explicit trace/request identifiers associate events; one primary group supplies diagnosis
evidence, while other groups remain visible in the dashboard and report exports.

## Current status

Optional [NeMo Guardrails policy checks](docs/guardrails.md) support observation and
enforcement for prompt injection and credential disclosure in both diagnosis modes.

This repository is a working scaffold and MVP:

- deterministic localization implemented
- local index implemented with SQLite
- Temporal workflow and worker included
- LangGraph diagnosis graph included
- local LLM adapters included (Ollama and OpenAI-compatible endpoints)
- LiteLLM Gateway as the default LLM route through the existing OpenAI-compatible adapter
- Tree-sitter syntax-tree indexing via the optional `parsing` extra, with safe fallback
- React operations dashboard with saved incident history, evidence views, and configuration
- durable dashboard submissions and progress tracking through Temporal
- repository profiles, worker-side path validation, and isolated indexes for each dashboard run

It is designed to be extended, not treated as finished enterprise software.

## Dashboard quick start

With Docker Desktop running, copy `.env.example` to `.env` if needed and set a
private `LITELLM_MASTER_KEY` as described in [gateway setup](docs/litellm.md).
Then start the stack, including LiteLLM:

```bash
docker compose -f docker-compose.dev.yml up --build -d
```

Open **http://localhost:8080**. The React app is built into the API image. Temporal UI
is available at http://localhost:8233.

1. Open **Repositories** and add a **Local directory** or **GitHub** profile. Validate
   local worker access, or click **Check & sync** for GitHub. Private repositories
   support read-only tokens. See [repository sources and manual sync](docs/repositories.md).
2. The project directory is mounted read-only at `/repositories` by default. Use
   `/repositories` as the profile path and `docker-compose.dev.yml` as its Compose file
   to analyze this project. Set `IC_REPOSITORIES_DIR` before starting Compose to mount
   another source directory there instead.
3. Open **Configuration** to review the default LiteLLM endpoint/model alias and tune analysis limits.
4. Start **New analysis**, choose a repository, and paste or load error logs.
5. Follow activity stages, inspect hypotheses and source evidence, and export the report.

### Standalone Docker log collector

The collector also supports [bounded runtime evidence](docs/runtime-evidence.md):
passive logs, resources and dependency health, with optional Prometheus and Tempo adapters.

To collect errors from other Compose projects on the **same Docker host**, set a
shared `IC_LOG_INGEST_TOKEN` in the Incident Copilot `.env` and recreate its API
container. Find its network with `docker inspect incident-copilot-api --format
'{{json .NetworkSettings.Networks}}'`, then set `IC_COPILOT_NETWORK` to that network
name. Start the separately packaged collector:

```bash
docker compose -f docker-compose.collector.yml up --build -d
```

The collector reads existing Compose labels through the local Docker daemon; target
application Compose files require no changes. Open **Log sources**, choose a
discovered project, map it to an active repository, and enable collection. The
worker must be able to access that repository's source. Vector sends bounded
30-second error windows and nearby lines to the API; repeated errors update
Issues, with at most one investigation per fingerprint per hour. The collector
uses a disk buffer while the API is unavailable. Logs arriving before a mapping
is enabled are discarded. Docker socket access grants powerful host privileges,
even when the socket bind mount is marked read-only; restrict who can deploy or
modify the collector.

Opt-in [deployed-version linking](docs/deployed-versions.md) connects each application
service's errors to its container's declared Git commit. Investigations use that
exact source snapshot instead of the latest synced branch; unavailable source
keeps the incident saved with a clear failure reason.

Code graph evidence is enabled by default. The application prepares each repository's
graph automatically, waiting up to five minutes before falling back to normal diagnosis.
Use a clean, committed checkout. **Repositories** shows graph status and provides
Prepare/Refresh actions; no additional Docker commands or token setup are required.

The dashboard starts empty and calculates all metrics from saved incidents. LLM
failures fall back to deterministic diagnosis, with the actual engine shown in the report.

**Error intelligence** and **Issues** track recurring error occurrences from new
dashboard uploads, independently of diagnosis completion. Explore hotspots and
trends, assign owners, resolve issues, and correct grouping with merge/split actions.
Exact duplicate uploads and reruns share occurrences; saved history is not backfilled.
See [uploaded error intelligence, counting, and triage](docs/error-intelligence.md).

### Run the dashboard on the host

Install Python dependencies as described below, and use Node.js **22.12 or newer**:

```bash
cd apps/web
npm ci
npm run build
cd ../..
```

Start Temporal and LiteLLM with `docker compose -f docker-compose.dev.yml up -d temporal litellm`.
Set `IC_TEMPORAL_HOST=localhost:7233` and
`IC_OPENAI_COMPATIBLE_BASE_URL=http://localhost:4000/v1` in the environment of **both**
the API and worker, use the gateway key for `IC_OPENAI_COMPATIBLE_API_KEY`, and
start each in a separate terminal:

```bash
python apps/worker/main.py
uvicorn apps.api.main:app --host 127.0.0.1 --port 8080
```

Open http://localhost:8080. For frontend development, run `npm run dev` from
`apps/web` and open http://localhost:5173; Vite proxies API requests to port 8080.
When the worker runs on the host, repository profiles use host filesystem paths.

### Configuration and persistence

- `IC_CONTROL_DB_PATH` selects the shared SQLite control store, defaulting to
  `data/control.db`. API and worker must use the **same file**. This bootstrap path
  can be supplied through the environment or the project `.env` file.
- The control store holds profiles, incident inputs/results, and immutable settings
  revisions, including server-side credentials. Keep its directory private. Runtime
  data, `.env`, and frontend build/dependency outputs are excluded from Git.
- Saved dashboard overrides take precedence over `IC_*` environment settings.
  **Reset** removes an override and restores its environment or built-in default.
  Application settings are not loaded from `configs/`. The LiteLLM service
  reads its own mounted `configs/litellm.yaml`.
- LLM and analysis changes affect new runs. Application, Temporal, and artifact
  storage changes remain pending until API and worker restart. Activation is deferred
  while incidents are queued or running; the UI shows saved and active values.
- Finish active runs, then use `docker compose -f docker-compose.dev.yml restart api worker`
  to activate infrastructure changes. Restart both host processes when running locally.
  Changing the Temporal address requires that server and namespace to exist already.
- **Cancel analysis** stops an undispatched incident immediately or asks Temporal to
  cancel a dispatched run. This lets you clear locally queued incidents before
  correcting an unreachable Temporal address. Dispatched cancellations need a worker
  to acknowledge them.
- Each dashboard run has an isolated index below the configured index path’s parent
  and JSON/Markdown artifacts under `IC_DATA_DIR/incidents/<id>`. Old artifacts stay
  at their original locations when settings change. Back up the control store and
  artifact directories together while API and worker are stopped.
- API keys are redacted from settings responses, incident responses, and report exports.
  Temporal dashboard payloads contain incident IDs and stage names rather than credentials.
- Repository validation executes on the worker. LLM/storage connection checks run
  from the API process; ensure those same endpoints and directories are reachable
  from the worker as well.
- The application is a trusted local console without authentication. Compose binds
  its published ports to localhost.

### Dashboard API and checks

The typed `/api` endpoints cover repository profiles, incidents and exports, overview
metrics, settings, and connection checks. `/api/incidents` accepts a repository ID,
logs, an optional title, and service hint; it returns `202` immediately after saving.
Unavailable Temporal services leave incidents saved and queued for automatic dispatch.
The original `/health`, synchronous `/diagnose`, and CLI entry points remain available.
Interactive API documentation is at `/docs`.

```bash
python -m pytest -q
cd apps/web
npm test
npm run build
```

An opt-in integration test starts a real temporary Temporal dev server and checks
worker validation, progress queries, duplicate dispatch recovery, and durable reports.
It requires network access for the official Temporal binary on its first run:

```powershell
$env:IC_RUN_TEMPORAL_TESTS = '1'
python -m pytest tests/test_temporal_integration.py -q
```

On POSIX shells, prefix that pytest command with `IC_RUN_TEMPORAL_TESTS=1` instead.

## Quick start

### 1) Install dependencies

Using `uv`:

```bash
uv sync
```

Or using pip:

```bash
pip install -e ".[dev,api,temporal,llm,agentic,parsing]"
```

Per-service requirements files are also available:

```bash
pip install -r apps/cli/requirements.txt
pip install -r apps/api/requirements.txt
pip install -r apps/worker/requirements.txt
```

Read-only iterative investigation is available behind `IC_AGENTIC_ENABLED=true`.
See [agentic investigation](docs/agentic-investigation.md) for limits, checkpoint recovery, and evidence tools.

### 2) Start Temporal + worker locally

A dev compose file includes Temporal, Postgres, Temporal UI, LiteLLM Gateway, and
the Incident Copilot API and worker. Configure `LITELLM_MASTER_KEY` first:

```bash
docker compose -f docker-compose.dev.yml up -d
```

### 3) (Optional) Run the worker manually instead

```bash
python apps/worker/main.py
```

### 4) Submit an incident from the CLI

```bash
python apps/cli/main.py diagnose \
  --repo-root /path/to/monorepo \
  --compose-path /path/to/monorepo/docker-compose.yml \
  --log-file /path/to/error.log
```

### 5) Or use the API

```bash
uvicorn apps.api.main:app --reload --port 8080
```

## Default LLM gateway

LiteLLM Gateway starts with the main Compose stack. Both single-pass diagnosis and
iterative investigation use its stable `incident-diagnosis` model alias. By default,
the gateway routes to Ollama with `llama3.1:8b` at `http://host.docker.internal:11434`.

Example `.env` values:

```env
LITELLM_MASTER_KEY=sk-<generated-value>
LITELLM_MODEL=ollama_chat/llama3.1:8b
LITELLM_API_BASE=http://host.docker.internal:11434
LITELLM_API_KEY=ollama
IC_LLM_PROVIDER=openai_compatible
IC_OPENAI_COMPATIBLE_BASE_URL=http://litellm:4000/v1
IC_OPENAI_COMPATIBLE_MODEL=incident-diagnosis
IC_OPENAI_COMPATIBLE_API_KEY=${LITELLM_MASTER_KEY}
```

Generate a private gateway key as described in the setup guide, then run:

```bash
docker compose -f docker-compose.dev.yml up --build -d
```

The API and worker wait for the gateway to become healthy. Saved dashboard model
overrides still take precedence; reset those fields to use the defaults. Host
processes use `http://localhost:4000/v1`. See
[LiteLLM setup, provider examples, and connection checks](docs/litellm.md).

## Managed code graph knowledge

GitNexus can enrich incident evidence with bounded static callers and callees while
preserving deterministic candidate ranking. Its managed service starts with the normal
stack, prepares a separate index for each repository, and refreshes after commits.
Read-only source mounts and persistent external graph storage are retained. See
[management, freshness rules, and comparison workflow](docs/code-graph.md).

## Project layout

- `apps/cli`: CLI entrypoint
- `apps/api`: FastAPI entrypoint
- `apps/worker`: Temporal worker entrypoint
- `src/incident_copilot/indexing`: service map and repo indexing
- `src/incident_copilot/correlation`: log parsing and candidate ranking
- `src/incident_copilot/analysis`: deeper language-aware analysis
- `src/incident_copilot/graph`: LangGraph diagnosis pipeline
- `src/incident_copilot/workflows`: Temporal workflows and activities
- `src/incident_copilot/reporting`: JSON / Markdown renderers

## Notes

- The index is stored under `data/indexes/repo_index.db` by default.
- The `parsing` extra enables Tree-sitter indexing for Python, JS/JSX, TS/TSX, Go,
  Java, JSON, and YAML. If a parser is unavailable, indexing falls back to Python
  AST or lightweight parsing. See [indexing details](docs/indexing.md).
- The LLM is optional. If unavailable, the graph falls back to deterministic hypothesis generation.
