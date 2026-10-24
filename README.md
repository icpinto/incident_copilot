# Incident Copilot

A diagnosis-first incident copilot for containerized monorepos.

This version intentionally **does not patch code**. It does:

1. Ingest user-provided error logs
2. Parse a local monorepo and docker-compose metadata
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

## Current status

This repository is a working scaffold and MVP:

- deterministic localization implemented
- local index implemented with SQLite
- Temporal workflow and worker included
- LangGraph diagnosis graph included
- local LLM adapters included (Ollama and OpenAI-compatible endpoints)
- Tree-sitter syntax-tree indexing via the optional `parsing` extra, with safe fallback
- React operations dashboard with saved incident history, evidence views, and configuration
- durable dashboard submissions and progress tracking through Temporal
- repository profiles, worker-side path validation, and isolated indexes for each dashboard run

It is designed to be extended, not treated as finished enterprise software.

## Dashboard quick start

With Docker Desktop running:

```bash
docker compose -f docker-compose.dev.yml up --build -d
```

Open **http://localhost:8080**. The React app is built into the API image. Temporal UI
is available at http://localhost:8233.

1. Open **Repositories**, add a profile, and validate worker access.
2. The project directory is mounted read-only at `/repositories` by default. Use
   `/repositories` as the profile path and `docker-compose.dev.yml` as its Compose file
   to analyze this project. Set `IC_REPOSITORIES_DIR` before starting Compose to mount
   another source directory there instead.
3. Open **Configuration** to select the LLM provider/model and tune analysis limits.
4. Start **New analysis**, choose a repository, and paste or load error logs.
5. Follow activity stages, inspect hypotheses and source evidence, and export the report.

The dashboard starts empty and calculates all metrics from saved incidents. LLM
failures fall back to deterministic diagnosis, with the actual engine shown in the report.

### Run the dashboard on the host

Install Python dependencies as described below, and use Node.js **22.12 or newer**:

```bash
cd apps/web
npm ci
npm run build
cd ../..
```

Start Temporal, set `IC_TEMPORAL_HOST=localhost:7233` in the environment of **both**
the API and worker, and start each in a separate terminal:

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
  The YAML files in `configs/` are examples, not additional runtime settings sources.
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
pip install -e ".[dev,api,temporal,llm,parsing]"
```

Per-service requirements files are also available:

```bash
pip install -r apps/cli/requirements.txt
pip install -r apps/api/requirements.txt
pip install -r apps/worker/requirements.txt
```

### 2) Start Temporal + worker locally

A dev compose file is included for Temporal, Postgres, Temporal UI, and the Incident Copilot worker:

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

## Suggested local LLM setup

Use Ollama with `llama3.1:8b` at `http://host.docker.internal:11434`.

Example `.env` values:

```env
IC_LLM_PROVIDER=ollama
IC_OLLAMA_BASE_URL=http://host.docker.internal:11434
IC_OLLAMA_MODEL=llama3.1:8b
```

The application uses the `IC_` prefix for environment variables. The Docker worker
uses these values by default and accepts overrides from the environment or `.env`.
When running directly on the host, set `IC_OLLAMA_BASE_URL=http://localhost:11434`
if Ollama is listening locally.

## Optional code graph knowledge

GitNexus can enrich incident evidence with bounded static callers and callees while
preserving deterministic candidate ranking. It runs in an optional isolated service,
with read-only source mounts and persistent external graph storage. See
[setup, freshness rules, and comparison workflow](docs/code-graph.md).

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
