# Structured causal reasoning and selective review

The current model and gateway can use three diagnosis policies through Configuration >
Diagnosis reasoning or `IC_LLM_REASONING_MODE`. The default remains `baseline`.

| Mode | Behavior |
| --- | --- |
| `baseline` | Existing evidence-constrained diagnosis without an extra review call. |
| `structured` | Shared evidence, mechanism, alternatives, distinguishing-check and conclusion instructions within existing calls. |
| `structured_review` | Structured reasoning plus at most one evidence review for competing causes, cited contradictions or an explicit causal gap. |

The model writes concise causal explanations into the existing hypothesis and action-plan
fields. These are evidence-linked assessments, not access to a model's private reasoning.
Candidate localization, provider routes, temperature and confidence semantics are unchanged.

## Budgets, failure and recovery

Single-pass review uses at most two diagnosis calls. Enhanced generation and review share
315 seconds; review itself is capped at 60 seconds. Iterative investigation retains its
four diagnosis calls, three retrieval rounds, twelve requests and 180-second deadline.
Useful retrieval takes priority; review uses only an available final call. Guardrail model
calls retain their separate existing accounting and policy budget.

Review sees the validated draft and original observations, preserving all cited evidence
and bounding other context to the 32 KiB model-input limit. It can qualify, remove or rerank
existing causes, or abstain. Retained hypotheses keep their title and original-rank identity,
contrary citations and essential evidence gaps. Application code validates identities,
ranks, citations and action plans. This checks provenance and contract consistency;
causal correctness still requires independent verification.

Invalid output, provider failure or review timeout retains the latest valid draft and
records a safe review outcome. Enforced guardrail failures reject the reviewed output.
Cancellation propagates. Iterative reviews use durable call reservations and cached results;
an interrupted outstanding review is marked failed on resume instead of consuming another
call. Mode and prompt version are pinned per execution. Existing executions resume as
baseline, and historical reports load without reasoning metadata. No stored-data rewrite
is needed. New settings apply through the existing immutable settings revisions.

Reports, the dashboard and JSON/Markdown exports expose reasoning mode, prompt version,
review outcome, model calls and separate contrary-evidence citations. Validation errors
from review have stage `diagnosis_review`. Report confidence remains unavailable;
`model_score` remains an uncalibrated self-assessment.

## Run paired comparisons

Use a clean, committed target repository and pin the actual upstream model version behind
the gateway alias. The benchmark indexes/localizes once, runs all three policies separately
in single-pass and iterative mode, verifies the source revision and candidate ranking, and
records a shared initial-evidence and policy fingerprint. Policy order is deterministically
shuffled per incident to distribute warm-up effects. Runbooks are loaded once for all arms. Iterative arms can retrieve different later
observations; those differences are part of each policy's behavior, not equal final context.

```powershell
python -m incident_copilot.evaluation.benchmark_runner --repo-root <clean-repository> --log-file <incident.log> --reasoning-modes --model-version <upstream-version> --output comparison.json
python -m incident_copilot.evaluation.diagnosis_review template --benchmark comparison.json --inference-mode single_pass --reasoning-mode baseline --output baseline-review.jsonl
```

Repeat template export for `structured` and `structured_review`, and separately for
`iterative`. Each arm includes its pre-review hypothesis snapshot (`draft_report`) for human comparison;
post-review execution metadata is omitted from that snapshot.
Templates copy measured latency, model version, source revision and evidence fingerprint;
they leave correctness and claim annotations unknown. Existing `template --report` and
`evaluate --reviews` commands remain available.

Create development cases for prompt iteration and a separate held-out set of at least
50 independently adjudicated incidents, including at least 15 whose supplied evidence is
insufficient. Verify mechanisms against independently established incident facts. Mark
`dataset_split: held_out`, `case_origin: verified`, and `independently_adjudicated: true`
only after that review. Fill `evidence_sufficient`, hypothesis `causal_correct`,
`unsupported_claims`, `claim_count`, and, for completed reviews, `draft_causal_correct`.
Review the original draft in the artifact when labeling the last field. Keep unresolved
labels null. Combine unique incident records into one JSONL file per inference mode and
policy. Synthetic cases and development data have separate files and cannot pass gates.

```powershell
python -m incident_copilot.evaluation.diagnosis_review evaluate --reviews structured.jsonl --output metrics.json
python -m incident_copilot.evaluation.diagnosis_review compare --baseline-reviews baseline.jsonl --structured-reviews structured.jsonl --review-reviews structured-review.jsonl --output promotion.json
```

The comparison requires matching incident IDs, upstream model versions, source revisions,
inference modes, evidence fingerprints and sufficiency labels. It reports exclusions and
uncertainty (Wilson intervals for proportions and a deterministic paired bootstrap for
causal-success gain). Success includes all judged evidence-sufficient incidents: an
abstention is a miss. Separate metrics report unsupported claims, false causal assertions
on insufficient evidence, abstention correctness and improved/damaged review drafts.

Pilot gates require at least five percentage points of causal-success gain over baseline,
no increase in unsupported claims or false causal assertions, mean latency at most 1.5x
and p95 latency at most 2x baseline. Review must also improve on structured prompting alone
and have actual draft labels. Missing or incomparable data cannot pass. The evaluator
recommends the simplest passing policy and never changes settings automatically.

No independently labeled production corpus or quality improvement is supplied by this
change. Keep baseline active until your held-out evaluation supports promotion. A passing
pilot is evidence for further monitored use, not proof of calibrated confidence.
