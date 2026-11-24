# Incident analysis workflow

The dashboard uses four durable Temporal stages. Each stage persists evidence in the
shared control store; the reasoning stage selects one of two LangGraph flows.

| Temporal stage | Functionality |
| --- | --- |
| `indexing` | Validate/resolve source, build service map, create or copy an isolated repository index. |
| `correlating` | Parse logs, associate events, select the primary group, rank candidate files/functions, and read snippets. |
| `analyzing` | Deep-analyze top candidates and prepare/retrieve optional static code-graph context. |
| `reasoning` | Gather available runtime evidence, run the selected graph, validate diagnosis, and save JSON/Markdown reports. |

Default LangGraph flow:

```text
normalize_logs → select_candidates → enrich_candidates
  → build_hypotheses → finalize_report → END
```

Optional iterative flow:

```text
assess → control → retrieve → merge → assess (or next pending retrieve)
  → optional final_assess → review → finalize → END
```

The iterative controller can finish without retrieval. `review` is a registered node
whose model call depends on policy and remaining capacity. The fixed graph does not
wrap the iterative graph. Temporal retries recover execution; the iterative loop
gathers more evidence.

Start with the [architecture handbook](architecture.md), then read
[Temporal stages and entry-point differences](architecture/temporal-workflows.md) and
[LangGraph node/state contracts and exact transitions](architecture/langgraph-and-patterns.md).
CLI and synchronous `/diagnose` share analysis services but do not dispatch the
dashboard Temporal workflow. Manual responder actions happen after analysis.
