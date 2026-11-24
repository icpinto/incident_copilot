# Bounded evidence investigation

Incident Copilot can optionally run an iterative investigation inside LangGraph. Initial indexing, candidate localization, and candidate scores remain deterministic. The model assesses provisional explanations, requests additional evidence, and revises its diagnosis. Every request is validated and executed by application code against the configured repository and index.

For the implemented design, see the [architecture handbook](architecture.md),
[node/state contracts](architecture/langgraph-and-patterns.md), and
[memory/context architecture](architecture/memory-and-context.md).

Enable **Iterative evidence retrieval** in Configuration, or set `IC_AGENTIC_ENABLED=true`. The feature defaults to disabled, preserving the existing single-pass diagnosis. Both Ollama and OpenAI-compatible providers use validated JSON decisions; provider-native tool calling is not required. Install the optional dependencies with `pip install -e ".[llm,agentic]"` to enable the LangGraph runtime and SQLite checkpointer. Missing dependencies produce an explicit fallback reason.

## Evidence and stopping

The available operations find indexed symbols, read bounded source ranges, search literal source text, retrieve file metadata, and look up GitNexus callers or callees. The symbol index records definitions and ranges; it is not a full data-flow analysis. Managed GitNexus is enabled by default and prepares a matching repository graph automatically; external mode requires an already prepared service. An unavailable or partial graph is recorded with its limitations. See [code graph management](code-graph.md).

The investigator can follow a newly discovered helper, inspect relevant test assertions, compare competing explanations, or search when initial localization finds no candidates. Each request identifies the evidence question it addresses. Reports show citations, retrieved source, investigation actions, and unresolved evidence gaps.

The default and maximum budgets are:

| Limit | Value |
|---|---|
| Retrieval rounds | 3 |
| Requests per round | 4 |
| Total evidence requests | 12 |
| Assessment model calls | 4 |
| Investigation deadline | 180 seconds |
| Source response | 80 lines or 8 KiB |
| Total retrieved content | 64 KiB |
| Serialized model input | 32 KiB |
| Related graph symbols | 20 across the investigation |

`IC_AGENTIC_MAX_ROUNDS`, `IC_AGENTIC_MAX_TOOL_REQUESTS`, and `IC_AGENTIC_TIMEOUT_SECONDS` can lower the configurable budgets. The controller stops when the model finishes, a budget is exhausted, requests repeat, or retrieval adds no evidence. Clipped content is marked; the model can request a subsequent bounded range while budget remains.

Stopping retrieval is separate from finishing the diagnosis. Empty or duplicate-only
rounds and retrieved-content limits run a final assessment when the original deadline
and model-call budget permit. The assessment receives request questions and scope,
outcome status and reason, retained evidence IDs, and clipping/omission limitations.
It must finish without requesting more evidence. An empty search describes only its
searched scope; unavailable telemetry leaves the observation unknown.

The last available assessment call, and assessments after round/request limits,
also use this finish-only contract. Retrieval cannot start without capacity for a
subsequent assessment. These calls share the existing four-call maximum and
180-second deadline; optional review uses only remaining capacity. Reports preserve
the retrieval stop reason. `unassessed_request_ids` lists outcomes not included in a
validated assessment, with an explicit unresolved gap in the dashboard and exports
if time, calls, invalid output, or provider failure prevents assessment. Delivery to
a validated assessment does not establish that the model interpreted it correctly.

Retrieval is read-only. It does not execute tests, change source, or deploy code. Deployment-linked incidents can also retrieve [bounded runtime evidence](runtime-evidence.md) through the Docker collector. Runtime clients and credentials stay outside graph checkpoints. Source requests remain constrained to the trusted indexed repository; a changed or unverified source snapshot disables source retrieval. Runtime-enabled incidents can continue with runtime evidence and an explicit source gap. Reports retain graph completeness and parser limitations. A valid citation identifies an observation; it cannot prove causation.

## Recovery and reporting

Completed graph steps are persisted with a SQLite checkpointer under the shared data directory, keyed by execution ID. Checkpoints include evidence, validated decisions, request reservations, consumed budgets, and the original deadline. A worker retry resumes completed work without resetting those limits. The deadline includes downtime. An interrupted external request may repeat because the remote service may have completed it before its result was saved.

Temporal keeps its four stages. The reasoning stage runs the graph and sends heartbeats; cancellation propagates while waiting on model or retrieval calls. The same investigation service is used by dashboard, synchronous API, CLI, and legacy Temporal entry points when trusted runtime context is available.

A later model failure preserves the latest valid hypotheses and marks the investigation incomplete. If no valid assessment exists, the deterministic fallback remains available. Old reports without investigation fields continue to render. JSON and Markdown exports include investigation evidence and citations alongside the diagnosis.

Validation failures also appear in the incident's **Analysis validation errors** panel, including while analysis is running. Reports and streamed evidence include a typed `validation_errors` list with the stage, failure kind (`json`, `schema`, `semantic`, or `limit`), model call or evidence request, and individual field paths, codes, and explanations. Invalid JSON, missing or mistyped fields, numeric bounds, inconsistent decisions, invalid citations, and evidence response validation are reported separately. Earlier valid hypotheses remain available when a later response fails validation. Checkpoints and exports preserve these diagnostics. Each failure includes up to 30 issues and the total issue count; rejected values and raw exception context are excluded.

## Comparing outcomes

Run `python -m incident_copilot.evaluation.benchmark_runner --repo-root <path> --log-file <logs> --output <report.json>` to generate the paired benchmark. It indexes and localizes once, retains the existing graph-enrichment comparison, and adds an iterative arm starting from the same graph-enriched evidence as the single-pass arm.

The artifact records latency, model and evidence requests, retrieved content size, citation validity, and evidence counts. Citation validity checks that a reference resolves to collected evidence; it does not judge whether that evidence supports the claim. Review hypotheses against labeled source and incident facts to record diagnosis quality and unsupported claims. More retrieval alone does not establish improvement.
