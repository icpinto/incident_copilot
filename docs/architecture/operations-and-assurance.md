# Operations, failure handling, and assurance

[Architecture handbook](../architecture.md) · [Previous: Agent harness](agent-harness.md)

This chapter connects architecture to deployment and verification. Use the linked
feature guides for commands; the values here describe checked-in behavior, not private
environment files or a live installation's saved overrides.

## Configuration and public contracts

Saved dashboard overrides take precedence over `IC_*` environment values and built-in
defaults. `Configuration.revision()` stores the effective analysis settings for each
new run. Model/analysis edits affect subsequent runs; retries retain their captured
revision. Resetting an override restores environment/default behavior.

Infrastructure fields include application/environment identity, data/index/cache
paths, and Temporal connection settings. These activate on process startup only when
there is no active work blocking activation. The UI distinguishes desired and active
values. A transactional guard prevents stale processes from submitting work against
another activated infrastructure configuration. The control-database bootstrap path
is independent of artifact storage.

Langfuse process settings and LiteLLM's mounted YAML are separate from analysis
settings. Application configuration is not loaded from `configs/`; files there belong
to their respective services. API and worker must resolve the same shared control
store and infrastructure settings.

| Setting / capability | Repository default | Interpretation |
| --- | --- | --- |
| `IC_AGENTIC_ENABLED` | `false` | Fixed diagnosis; iterative evidence retrieval is opt-in |
| `IC_LLM_REASONING_MODE` | `baseline` | Structured prompting/review are independent opt-in policies |
| `IC_TOP_K_CANDIDATES` / `IC_DEEP_ANALYSIS_TOP_N` | 10 / 5 | Localization and deeper-analysis bounds |
| Iterative rounds / requests / deadline | 3 / 12 / 180 seconds | Configurable downward; four assessment/review calls maximum |
| `IC_GUARDRAILS_MODE` / timeout | `observe` / 10 seconds | Policy findings recorded without enforcement by default |
| `IC_CODE_GRAPH_ENABLED` / mode | `true` / `managed` | Automatic per-repository preparation and verified retrieval |
| Graph preparation / retrieval | 300 / 15 seconds | Separate deadlines; graph failure preserves normal diagnosis |
| Provider / model alias | `openai_compatible` / `incident-diagnosis` | Compose endpoint `http://litellm:4000/v1`; host built-in endpoint `http://localhost:4000/v1` |
| Gateway upstream | `ollama_chat/llama3.1:8b` | Compose default, operator can route the alias elsewhere |
| Gateway timeout / HTTP client timeout | 300 / 315 seconds | Outer investigation/review deadlines may cancel sooner |
| Langfuse / content capture | Disabled / disabled outside overlay | Overlay enables tracing; content capture remains opt-in |
| Docker collector | Separate deployment | Needs enabled log-source mappings and shared ingest authentication |

Public interfaces remain typed FastAPI endpoints and Pydantic contracts:

- `/api/incidents` creates saved asynchronous analyses; incident reads expose stage,
  evidence, report, validation, reasoning, and optional investigation metadata.
- `/diagnose` returns a synchronous `DiagnosisReport`; CLI uses the same report model.
- Repository, settings, graph preparation, log-source/runtime, Issues, exports, and
  manual action-event endpoints are registered under `/api`; `/docs` exposes OpenAPI.
- Model-facing `InvestigationDecision`, `EvidenceRequest`, `ToolOutcome`, and evidence
  items are internal validated contracts, not permission to run arbitrary API actions.
- JSON and Markdown preserve provenance, gaps, optional subsystem summaries, and
  compatibility with historical reports. New architecture documentation changes none
  of these interfaces.

## Deployment and persistence

| Deployment | Components / boundary |
| --- | --- |
| Base `docker-compose.dev.yml` | API/React UI on localhost:8080; Temporal on 7233 and UI on 8233; PostgreSQL; LiteLLM on 4000; managed GitNexus on 3111 |
| Collector Compose | Separate Vector/helper deployment sharing the selected application network; Docker socket and durable collector buffer remain here |
| Langfuse overlay | Web/worker with PostgreSQL, ClickHouse, Redis, MinIO; web port defaults to localhost:3000 |
| Host API/worker/CLI | Host-accessible paths and endpoints required; not equivalent to Docker service names/mount paths |

The base stack binds `./data` into API and worker, mounts source read-only at
`/repositories`, and uses named `repository-cache`, `gitnexus-state`, and
`gitnexus-auth` volumes. Repository cache is worker-writable and API/graph-readable.
GitHub managed graph preparation requires identical absolute cache visibility in the
worker and graph service.

The checked-in base PostgreSQL service has no explicit persistence volume. Temporal
durability therefore depends on retaining that database/container storage; recreating
or removing it is different from restarting processes. This documentation does not
change that deployment. Langfuse and collector define their own persistent volumes.

For a consistent application backup, stop API/worker and other writers, and retain the
control database, artifact/investigation directories, isolated indexes, repository
cache, and graph/auth volumes together. Temporal execution history requires a separate
PostgreSQL backup if in-flight workflow recovery is required. Trace retention requires
backing up Langfuse separately. A control-store backup alone cannot reconstruct all
source, graph, checkpoint, or artifact references.

There is no automatic cache pruning or general incident/checkpoint retention service.
Old runs keep original paths after settings changes. Never remove cache or journal
files needed by active/resumable work. The local console has no application login,
tenant separation, or encryption at rest; source mounts and localhost publication do
not make its private database safe to share.

Setup references: [dashboard](../../README.md#dashboard-quick-start),
[repositories](../repositories.md), [code graph](../code-graph.md),
[gateway](../litellm.md), [runtime evidence](../runtime-evidence.md),
[tracing](../langfuse.md).

## Failure and recovery behavior

| Scenario | Expected implemented behavior |
| --- | --- |
| Temporal unavailable at submission | Incident stays saved/queued; reconciler retries stable dispatch. |
| Start accepted but acknowledgement lost | Duplicate workflow ID is recognized; no second dashboard workflow created for that incident. |
| Worker dies during activity | Temporal can retry; completed stage evidence remains saved. Iterative checkpoints/journal retain completed work, reservations, and original deadline. |
| Native parser crashes or grammar fails | Isolated parser falls back for that grammar; valid remaining language work can continue. |
| Graph stale, dirty source, missing symbol, graph deadline/outage | Record graph status/reason and continue without that enrichment. |
| Source-only iterative snapshot unverified | Explicit downgrade to fixed diagnosis with an incomplete investigation summary. |
| Deployment-linked source unavailable | Explicit source failure; runtime-enabled incidents can proceed with source tools disabled and a source gap. |
| Source or graph changes during investigation | Reject mixed snapshot/generation evidence; source-only investigation stops. Runtime-aware routing can disable source tools and continue independent runtime retrieval. |
| Model request fails or output is invalid | First failure yields deterministic insufficient evidence; later iterative failure preserves the latest validated assessment with an incomplete marker. |
| Retrieval empty, duplicate, unavailable, or clipped | Preserve typed outcome and scope; bounded controller stops/reassesses as applicable; unassessed outcomes become explicit gaps. |
| Guardrail rejects or cannot inspect | Observe records and continues; enforce rejects that model step and retains earlier accepted findings when present. |
| Review fails or times out | Keep accepted draft and record review failure; never accept rejected reviewed output. |
| Runtime backend unavailable | Record missing observation and collection outcome; do not invent health or replace original containers. |
| Langfuse unavailable | Diagnosis continues; traces/counts may be missing. |
| Cancellation | Stop further work as cancellation propagates; dispatched workflow needs a worker acknowledgement. |

Raw provider exception bodies and rejected model values are excluded from public
diagnostics. Inspect safe reason codes, validation fields, collection outcomes, and
actual engine markers before assuming that an LLM or optional service ran successfully.

## Diagnosis quality and human responsibility

Three different concepts must remain distinct:

1. Candidate `score` is deterministic localization strength.
2. `model_score` is a raw, uncalibrated model self-assessment.
3. Report `confidence` is nullable diagnosis probability; new analyses return null
   with `confidence_status="unavailable"`. No runtime calibrator is enabled.

`provisional` requires at least one evidence-backed causal hypothesis;
`insufficient_evidence` is a valid outcome, possibly with tentative leads.
`unknown` supports historical reports. A successful insufficient-evidence model
assessment keeps the LLM engine marker rather than becoming a provider failure.

Manual action events are append-only and idempotent by client event ID. Checks can
support/weaken a hypothesis or remain inconclusive. Mitigations can be applied,
verified, failed, or rolled back; verified recovery requires observations. Applied
does not imply recovery. Events do not change graph execution status, rewrite the
diagnosis, or automatically close recurring Issues.

Issues group recurring errors by deterministic fingerprints scoped to a repository.
Occurrence capture is independent of diagnosis success. Duplicate batches/reruns share
occurrences; owner/status/group corrections have activity history. Issue analytics
describe captured uploads/collector observations, not production-wide reliability.

See [confidence semantics](../diagnosis-confidence.md),
[recommendations](../actionable-recommendations.md), and
[error intelligence](../error-intelligence.md).

## Verification and evaluation map

| Scenario / property | Existing verification source |
| --- | --- |
| Fixed diagnosis, strict response contract, fallback | [LLM diagnosis tests](../../tests/unit/test_llm_diagnosis.py), [validation diagnostics](../../tests/unit/test_validation_diagnostics.py) |
| Iterative retrieval and state transitions | [Investigation tests](../../tests/unit/test_investigation.py), [tools](../../tests/unit/test_evidence_tools.py) |
| Budgets, final assessment, unassessed outcomes | [Limits](../../tests/unit/test_investigation_limits.py), [final assessment](../../tests/unit/test_investigation_final_assessment.py) |
| Worker retry, cached outcomes, persistent reservations | [Recovery tests](../../tests/unit/test_investigation_recovery.py), [Temporal integration](../../tests/test_temporal_integration.py) |
| Source identity, deployment linking, graph freshness | [Repository sync](../../tests/test_repository_sync.py), [deployed versions](../../tests/test_deployed_versions.py), [graph pipeline](../../tests/test_code_graph_pipeline.py) |
| Runtime-only and scoped telemetry behavior | [Runtime evidence tests](../../tests/test_runtime_evidence.py), [collector tests](../../tests/unit/test_runtime_collector.py) |
| Guardrail modes, fail-closed model acceptance | [Guardrail pipeline](../../tests/test_guardrails_pipeline.py), [policy tests](../../tests/unit/test_guardrails.py) |
| Model routing, telemetry isolation/propagation | [LiteLLM Compose](../../tests/test_litellm_compose.py), [observability](../../tests/unit/test_observability.py), [Langfuse Compose](../../tests/test_langfuse_compose.py) |
| Review identity, contrary evidence, evaluation arithmetic | [Review tests](../../tests/unit/test_diagnosis_review.py), [reasoning evaluation](../../tests/unit/test_reasoning_evaluation.py) |
| Responder outcomes and recurring-error lifecycle | [Recommendations](../../tests/test_actionable_recommendations.py), [error intelligence](../../tests/test_error_intelligence.py) |

Controlled transports/fixtures allow ordinary checks without Docker, an external
model, or telemetry backends. Real Temporal/GitNexus integration and Langfuse smoke
checks are opt-in and require their documented services. Frontend tests cover evidence,
validation, recommendations, runtime, graph, and reasoning displays.

Contract tests and synthetic scenarios establish behavior, bounds, and provenance;
they do not establish causal accuracy. The paired benchmark reuses initial localization
to compare graph/iterative/policy arms. Labeled human evaluation measures causal
correctness, unsupported claims, abstention, latency, and review damage/improvement.
Current promotion gates require matched held-out records, at least fifty independently
adjudicated incidents including fifteen insufficient-evidence cases, and the documented
quality/latency thresholds. The evaluator recommends a policy but does not activate it.
Keep baseline until evidence supports promotion; see [structured reasoning evaluation](../structured-reasoning.md).

## Architectural change checklist

For changes to this solution:

- Trace all affected entry points, not just the dashboard path.
- Update the relevant stage/node/tool/state contract and diagram in this handbook.
- Verify defaults against `Settings`, Compose, gateway configuration, and constants;
  distinguish startup configuration from per-run captured settings.
- Explain restart/retry behavior, source identity, persisted references, and historical
  compatibility when changing storage, prompts, or workflows.
- Keep optional/unavailable behavior visible; preserve insufficient-evidence outcomes
  and safe diagnostic boundaries.
- Update the existing feature guide and its operational commands when behavior changes.
- Review links/anchors and Mermaid rendering. Use existing focused tests for behavioral
  changes; do not add implementation-mirroring tests for prose-only edits.

Implementation references: [settings](../../src/incident_copilot/core/settings.py),
[configuration lifecycle](../../src/incident_copilot/dashboard/config.py),
[API contracts](../../src/incident_copilot/dashboard/models.py),
[report conversion](../../src/incident_copilot/reporting/report_builder.py),
[evaluation](../../src/incident_copilot/evaluation/).
