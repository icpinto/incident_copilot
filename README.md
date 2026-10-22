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
- optional Tree-sitter integration with safe fallback to regex/light parsing

It is designed to be extended, not treated as finished enterprise software.

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
- Tree-sitter is optional. The indexer will fall back to lightweight parsing if unavailable.
- The LLM is optional. If unavailable, the graph falls back to deterministic hypothesis generation.
