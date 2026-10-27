# Localization, diagnosis, and abstention

Candidate `score` is **localization strength**: a raw correlation score used to
rank source locations. It is not a probability. An exact stack-path match adds
32 points; identifying that location does not establish the cause of a failure.
Ranking weights and candidate reasons are unchanged.

New reports expose these fields through every API, JSON export, CLI, and workflow:

| Field | Meaning |
| --- | --- |
| `diagnosis_status` | `provisional` means at least one evidence-backed causal hypothesis; `insufficient_evidence` means the available evidence cannot support a cause. `unknown` is reserved for historical results without an explicit assessment. |
| `diagnosis_reason` | Why the assessment was made, including missing evidence. |
| `next_checks` | Report-level actions, available even when there are no hypotheses. |
| Hypothesis `confidence` | Nullable diagnosis probability. New analyses always return `null` in this release. |
| Hypothesis `confidence_status` | `unavailable`, `uncalibrated`, or `calibrated`. Numeric confidence may be treated as a probability only when explicitly `calibrated`. |
| Hypothesis `model_score` | Optional raw model self-assessment in [0, 1]. It is uncalibrated and must not be presented as a diagnosis probability. |

Generation contracts still call the model self-assessment `confidence` for
compatibility with saved investigation decisions. The report conversion moves it
to `model_score` and sets reported `confidence` to `null`, with status `unavailable`.
Models cannot supply report confidence metadata. No runtime calibrator is enabled.

An insufficient-evidence assessment may retain tentative causal leads or have no
hypotheses. Deterministic fallback supplies locations and next checks without an
invented causal hypothesis. Successful abstention keeps the LLM engine marker and
does not become a provider failure. Investigation completion and evidence
sufficiency are separate: a finished investigation may still lack causal evidence,
and an interrupted investigation may retain a provisional assessment.

Old numeric confidence loads as `uncalibrated`; old outcomes load as `unknown`.
Stored JSON and checkpoint files are not rewritten. Resumed old assessments retain
their raw model score, without manufacturing a calibrated probability. Dashboard
and Markdown percentage displays require explicitly calibrated confidence. JSON
clients must check `confidence_status` and handle `null` before doing arithmetic.

## Collecting labeled incidents

Start with saved reports and the exact incident input and repository snapshot used
to generate them. Export one review template per report:

```powershell
python -m incident_copilot.evaluation.diagnosis_review template --report report.json --repository-revision <revision> --model-version <provider-model-version> --prompt-version <prompt-revision> --output review.jsonl
```

Each JSONL record contains `incident_id`, `repository_revision`, `model_version`,
`prompt_version`, `diagnosis_status`, nullable `evidence_sufficient`, and a
`hypotheses` array. Each hypothesis contains `hypothesis_id`, `rank`, nullable
`model_score`, and nullable `causal_correct`. Exported hypothesis IDs combine the
incident ID and rank; they identify a hypothesis in that saved report, not across
investigation rounds. Templates copy model scores without filling any labels.

Review the report against independently verified incident causes, such as a
reproduction and a confirmed resolution. Label `causal_correct` true only when the
proposed mechanism explains the incident; a correct file match is insufficient.
Use false for a disproven mechanism and null for an unresolved or unjudgeable one.
Label `evidence_sufficient` true when the evidence supplied to that analysis can
support a causal diagnosis, false when it cannot, and null when review cannot
determine sufficiency. A cause learned later does not imply the original evidence
was sufficient. Keep model scores unchanged during review.

Combine records into a JSONL dataset with one unique incident ID per file. Evaluate
different comparison arms in separate files. Existing source-localization golden
cases remain localization tests; they are not causal correctness labels.

```powershell
python -m incident_copilot.evaluation.diagnosis_review evaluate --reviews labeled-incidents.jsonl --output metrics.json
```

The evaluator reports:

- Brier score over hypotheses with both a raw model score and a causal label,
  including tentative leads. This measures the raw scores; it does not calibrate them.
- Ten reliability bins: [0, .1), [.1, .2), and so on, with the final bin including 1.
  Each bin reports count, average score, and observed causal correctness.
- Non-abstention coverage: provisional assessments divided by provisional plus
  insufficient-evidence assessments; unknown outcomes are excluded.
- Top-ranked accuracy: correct rank-one hypotheses among non-abstaining incidents
  with an adjudicated rank-one label.
- Abstention correctness: insufficient-evidence assessments with a false
  `evidence_sufficient` label, divided by abstentions with a known sufficiency label.

Outputs disclose denominators, excluded observations, and unknown-label counts.
Empty denominators produce null metrics. Missing labels never count as incorrect.
The synthetic fixture verifies arithmetic only and supplies no calibration evidence.

Collect representative incidents, including misleading stack locations, competing
causes, and incidents that require abstention. Review results by model, prompt,
repository, and evidence availability before deciding how to fit a calibrator.
Fitting, held-out validation, versioned runtime artifacts, and activation thresholds
are deferred; diagnosis probabilities remain unavailable until that work is validated.
