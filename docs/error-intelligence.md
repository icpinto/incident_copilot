# Uploaded error intelligence

Open **Error intelligence** to compare occurrence counts and inspect hotspots. Use
**Issues** to assign an owner, acknowledge a recurring error, investigate it, or
record its resolution. The existing **Overview** and **Incidents** pages continue
to describe investigation execution, not application reliability.

## Coverage and counting

Tracking begins with the first startup of the updated control store. Its timestamp
is persisted and displayed in the new views. Existing investigations are not
backfilled. There is no telemetry collector or change to the upload form.

New dashboard submissions are parsed before repository indexing or diagnosis. Each
error-bearing parsed event contributes one occurrence, across every trace/request
group. A multiline exception chain is one occurrence. Informational records remain
available in the investigation's original logs. Counts remain available if Temporal
is offline or an investigation fails or is canceled.

The original UTF-8 input's SHA-256 digest and repository profile ID identify an
upload batch. Byte-identical uploads share occurrences, even when their titles or
service hints differ. Reruns link to the same batch and do not add occurrences.
Rerunning a historical, untracked investigation does not start tracking it. New,
partially overlapping uploads and inputs with different whitespace are separate
batches; this release does not deduplicate individual records across batches.

All metrics describe uploaded data. They are not production-wide error rates or
user-impact estimates. Service ownership comes from observed log metadata; user
hints and diagnosis predictions do not fill missing observations.

## Grouping and triage

Version 1 fingerprints use observed service, ordered exception types and normalized
messages, and ordered stack paths/functions, scoped to a repository profile.
Whitespace, timestamps, UUIDs, explicit request/trace identifiers, and standalone
numbers are normalized. Stack line numbers are excluded and path separators are
normalized. Without a parsed exception, grouping uses the event message and frames.
Grouping does not depend on a model, repository revision, or diagnosis outcome.

Issues support **open**, **acknowledged**, **investigating**, **resolved**, and
**ignored** states. Owners are free-text person or team labels. Saving a diagnosis
report never resolves an issue. Title, ownership, status, and resolution-note changes
appear in the issue activity history.

A newly captured occurrence reopens a resolved issue only if its effective time is
after resolution. Older timestamped uploads and duplicate batches cannot reopen
it. Ignored issues keep collecting observations without reopening.

Merge issues from the same repository into a selected target. The target keeps its
owner and status. Fingerprints route future occurrences to the target, old issue
links redirect, and the activity view retains merged history. Splitting moves a
nonempty proper subset of occurrences into a new open issue with the original
owner. Splitting changes selected records only; future errors still follow automatic
grouping. Neither correction triggers recurrence by itself.

## Time windows and hotspots

Timezone-aware source timestamps are converted to UTC. Missing, invalid, and
timezone-free timestamps use the original batch submission time. The occurrence
view labels this fallback, and analytics returns timestamp coverage counts. No
source timezone or clock-skew correction is guessed.

Analytics defaults to a rolling 14-day window and compares it with the immediately
preceding equal-length interval. Windows are start-inclusive and end-exclusive;
daily chart buckets use UTC, including partial boundary days. Custom windows must
be timezone-aware, positive, and at most 366 days. Percent change is unavailable
when the previous count is zero.

The issue list keeps older issues discoverable when no explicit time range is
selected, while its counts describe the default window. An explicit time filter
limits membership to issues observed in that window. Issue detail always exposes
all captured evidence and total counts.

Hotspots use repository, observed service, exception type, stack file, or function.
Unknown metadata is shown as `(unknown)`. Each occurrence contributes once per
distinct value in a dimension. Multiple exceptions or stack locations can place
one occurrence in multiple buckets, so those buckets are not additive. Observed
stack locations do not establish the cause of a failure.

## Dashboard API

- `GET /api/error-intelligence`: coverage, comparison totals, UTC trends, hotspots,
  and frequent/growing issues. `dimension` accepts `repository`, `service`,
  `error_class`, `file`, or `function`.
- `GET /api/issues`: issue search and pagination. `sort` accepts `occurrences`,
  `growth`, or `last_seen`.
- `GET/PATCH /api/issues/{id}`: details and updates to `title`, `owner`, `status`,
  and `resolution_note`.
- `GET /api/issues/{id}/occurrences`: paginated evidence and original-log links.
- `GET /api/issues/{id}/activity`: paginated lifecycle and grouping history.
- `POST /api/issues/{id}/merge`: `{ "source_issue_ids": ["..."] }`.
- `POST /api/issues/{id}/split`: `{ "occurrence_ids": ["..."], "title": "Optional title" }`.

Shared filters are `q`, `repository_id`, `service_name`, `error_class`, `file_path`,
`function_name`, `owner`, `status`, `incident_id`, `start_at`, and `end_at`.
Pages use `page` (starting at 1) and `page_size` (1–100, default 20). Occurrence
and activity views default to all history. Issue responses include canonical IDs
when an old link refers to a merged issue.

Incident responses add `error_batch_id` and `error_tracking_status` (`tracked`,
`duplicate`, or `untracked`). Historical responses default to untracked. Diagnosis
reports, exports, and existing overview APIs retain their contracts.

## Deployment

Restart the API on the updated application version. Schema creation is additive
and idempotent in the existing shared SQLite database; no separate service or
migration command is required. Issue capture and investigation creation share a
transaction. Grouping corrections and triage use write transactions; analytics
uses consistent read snapshots. Existing control-store backup procedures also
cover issue tracking. No new LLM calls or Temporal workflow stages are introduced.
