# Actionable recommendations

New diagnoses include `action_plan_version: 1`. Each hypothesis has an `id` and an
`action_plan`: cited supporting claims, missing evidence, one to three ordered manual
checks, runbook references, and up to two mitigation proposals. Expected results and
recovery criteria are predictions. They are not collected observations or calibrated
probabilities. Historical reports retain their original evidence and next-check views.

Checks state where to work, what evidence to collect, and results that support or weaken
the hypothesis. Optional commands show unresolved placeholders. All actions are manual.
An insufficient-evidence diagnosis proposes checks before mitigations.

Mitigations include prerequisites, risks, affected services, steps, rollback, and a
verification measure, success condition, and observation period. Missing operational
requirements mark a proposal **Blocked**. Unknown criteria remain explicit; responders
must establish them before acting. **Repository runbook** identifies procedure provenance,
not approval or proof of causation. Other proposals are **Unreviewed model proposal**.

## Versioned repository runbooks

Create Markdown files under `docs/runbooks/` in the repository being analyzed. Copy the
[template](runbooks/template.md), choose a unique ID, and fill every section. The
[dependency timeout example](runbooks/example-dependency-timeout.md) is illustrative.
Front matter contains exactly `id`, `title`, `services`, `error_classes`, and
`failure_layers`. Use Compose service names and exact exception classes. An empty
`error_classes` list matches all errors for the service; `services: ["*"]` permits a
generic runbook. Failure layers describe applicability; observed timeouts alone do not
identify a failure layer. Matching uses observed primary-group service/error metadata.
Specific service matches precede wildcard matches, then error matches, then file path.

Required level-two headings: Diagnosis, Prerequisites, Mitigation, Risks, Rollback, and
Verification. Files are UTF-8, at most 8 KiB each. Discovery scans at most 100 Markdown
files without following directory symlinks, selects at most three, and applies the shared
prompt budget. Invalid files, unsafe paths and omitted content produce limitations.
Runbook content is untrusted data: it cannot change model instructions or execute code.

Reports retain section excerpts, physical line ranges, source revision when known, and
the original file SHA-256. They use the worker's analysis snapshot, including deployed
commit pinning. Source-unavailable investigations continue without repository runbooks.
The UI and exports display **No matching runbook** instead of inventing documentation.

## Responder follow-through

The dashboard records append-only observations through:

- `GET /api/incidents/{id}/action-events`
- `POST /api/incidents/{id}/action-events`

POST accepts `client_event_id`, `action_id`, `status`, `observations`, optional
`responder`, and optional boolean `useful`. The client generates a new event identifier
for each submission and keeps it across retries. Repeated identical events return the
original record; reusing an identifier with different content returns 409. Unknown
actions or statuses return 422; outcomes require a saved report. The server records time.

Check statuses: `supports`, `weakens`, `inconclusive`, `blocked`, `skipped`.
Mitigation statuses: `in_progress`, `applied`, `verified`, `failed`, `rolled_back`, `skipped`.
`verified` requires recovery observations. Proceeding with a blocked mitigation requires
observations explaining how missing prerequisites were satisfied. Outcomes and names are
self-reported; this local console has no authenticated responder identity. Credentials
are redacted before persistence. Applied does not imply verified recovery.

Outcome history persists across refreshes. It does not rerank or rewrite the diagnosis,
close recurring Issues, or change pipeline execution status. Reruns create new incidents
with fresh outcome history. Dashboard JSON and Markdown downloads overlay current
`follow_up`; original diagnosis artifacts remain unchanged. CLI/synchronous reports have
recommendations but no connected dashboard outcome history.

## Pilot review

Use the dependency timeout, resource exhaustion and configuration-error fixtures in
`tests/fixtures/recommendations/scenarios.json` for repeatable contract checks. These
synthetic cases validate rendering and provenance; they do not establish model accuracy.
For representative real incidents, review claim support, usefulness of the first check,
runbook relevance, operational prerequisites, and independently observed recovery.
Record `useful` and observations in the dashboard. Time to the first informative check is
the first `supports`/`weakens` event's `recorded_at` minus incident `completed_at`; missing
outcomes remain unknown. Recovery and usefulness are responder reports, not automatic
proof. Compare real incidents before treating the feature as operationally validated.
