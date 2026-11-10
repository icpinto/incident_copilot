# Langfuse investigation tracing

Langfuse is optional. The normal Compose stack runs without it. The overlay adds a
local Langfuse 4.76.0 web/worker pair and dedicated PostgreSQL, ClickHouse, Redis,
and MinIO storage; its only published port defaults to `127.0.0.1:3000`. The MinIO image
comes from Chainguard because MinIO's former Docker Hub repository is unavailable.
If port 3000 is occupied, set `IC_LANGFUSE_HOST_PORT` in `.env.langfuse` to a free
port and set `IC_LANGFUSE_PUBLIC_URL` and `LANGFUSE_BASE_URL` to its localhost URL.

## Start locally

Generate bootstrap settings once from the repository root:

```powershell
python scripts/setup_langfuse.py --email you@example.com
```

This creates the ignored `.env.langfuse` file without printing credentials or
overwriting existing settings. The initial login password is in that private file.
Keep its project keys, database passwords, salt, and encryption key across restarts.

Start Docker Desktop, configure the normal `.env` (including `LITELLM_MASTER_KEY`
and the upstream model), then run:

```powershell
docker compose --env-file .env --env-file .env.langfuse -f docker-compose.dev.yml -f docker-compose.langfuse.yml up --build -d
```

Open <http://localhost:3000> and sign in with the generated initial account. The
organization, project and project keys are initialized automatically. Start an
incident in Incident Copilot, open its **Run details** tab and select **Open in
Langfuse**. Initial image downloads and ClickHouse migrations can take several
minutes. Allow at least 8 GiB of Docker memory for the combined development stack.

The API, worker and gateway deliberately do not depend on Langfuse health.
Unavailable telemetry must not stop incident analysis. Storage service dependencies
apply only inside the Langfuse stack. Volumes persist across `docker compose down`;
removing volumes deletes trace history and initialized accounts.

## What a trace contains

Each execution has a stable trace identity, persisted before workflow dispatch.
Activity retries and API/worker restarts retain it; rerunning creates a new incident
and trace. Temporal's interceptor propagates context through workflow headers, and
the application injects W3C `traceparent` into LiteLLM requests. Trace headers contain
only identifiers, never keys, evidence, or prompt content.

Application spans cover indexing, correlation, analysis, retrieval, diagnosis,
review, guardrail checks, and each model request. LiteLLM exports its full OTel v2
request tree beneath those request spans, including the generation with reported
usage and available cost. An app request span has no duplicate generation usage.
Direct Ollama mode records one application generation using `prompt_eval_count`
and `eval_count`; its cost and section-token estimates are unavailable.

`prompt.composition` observations describe the final clipped prompt: system,
logs, source, code graph, runtime evidence, runbooks, hypotheses/review draft,
schema and instructions. Evidence IDs/kinds identify individual retrieved items.
Counts use LiteLLM's authenticated local-tokenizer endpoint, with provider counting
disabled, in a bounded background pool (two workers, 32 pending jobs, three-second
job budget, one-second request timeout, 512 cached sections with a 60-second TTL). Failures are shown as
unavailable, never zero. Counts are estimates and may not sum to provider usage
because token boundaries, message wrappers and provider formatting differ. Request
schemas can incur additional provider overhead. The current clients do not stream,
so time to first token is unavailable.

## Content and credentials

Metadata-only capture is the default. To include redacted model inputs and outputs,
set `IC_LANGFUSE_CAPTURE_CONTENT=true` in `.env.langfuse`, then recreate API and worker
with the combined Compose command. Content is attached to the app's `llm.request`
span; the nested LiteLLM generation remains metadata-only. Counts always use the
actual transmitted content, before telemetry redaction.

Application telemetry scrubs configured credentials, repository tokens, recognizable
secret assignments, credential URLs and private keys before export. Export masking
also removes exception bodies, stack traces and raw third-party span events.
Redaction is best effort, not a guarantee that arbitrary PII or proprietary incident
content is removed; enable content capture only for evidence you intend to retain.

Langfuse settings are process environment settings, separate from saved analysis
configuration and Temporal payloads. Restart processes after changing them:

| Variable | Purpose |
| --- | --- |
| `IC_LANGFUSE_ENABLED` | Enable tracing; false outside the overlay |
| `IC_LANGFUSE_CAPTURE_CONTENT` | Capture redacted inputs/outputs; false by default |
| `IC_LANGFUSE_PUBLIC_URL` | Browser-facing URL used in incident links |
| `LANGFUSE_BASE_URL` | SDK endpoint; overlay uses `http://langfuse-web:3000` |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | Shared project credentials |
| `LANGFUSE_PROJECT_ID` | Project used for trace links |

Enabled executions are sampled at 100%. The optional dependency extra is
`pip install -e ".[observability]"`; Docker includes it. For host API/CLI processes,
load `.env` and `.env.langfuse` into the environment and use host-reachable gateway
and Langfuse URLs. The same settings can target an external Langfuse installation.

## Verification and troubleshooting

Run the focused observability tests and existing diagnosis/dashboard tests. The
opt-in real Temporal test checks headers, retries and replay behavior:

```powershell
$env:IC_RUN_TEMPORAL_TESTS="1"
python -m pytest tests/unit/test_observability.py -q
```

To verify a running local gateway and Langfuse instance, run:

```powershell
python scripts/smoke_langfuse.py
```

The smoke check makes one inference call, flushes traces, and polls Langfuse's
Observations API v2 for both the app request and gateway generation in one trace.
It prints identifiers and status only. It uses the configured upstream model.

If tracing is absent, check credentials, Langfuse health, the gateway's
`langfuse_otel` callback, `LITELLM_OTEL_V2=true`, and that inbound trace propagation is
enabled. Trace ingestion is asynchronous. Inspect service logs without enabling
verbose prompt logging. Historical incidents have no trace link until rerun.

To disable tracing, run the base Compose command without the overlay, recreating
API, worker and LiteLLM. Keep the Langfuse volumes to preserve history.

References: [Langfuse Compose](https://langfuse.com/self-hosting/deployment/docker-compose),
[headless initialization](https://langfuse.com/self-hosting/administration/headless-initialization),
[LiteLLM OTel v2](https://docs.litellm.ai/docs/observability/opentelemetry_v2).
