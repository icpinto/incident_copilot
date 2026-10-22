# Architecture

Temporal owns the durable workflow.
LangGraph owns bounded diagnosis reasoning.
Deterministic indexing and correlation happen before LLM inference.

The React dashboard is a static SPA served by FastAPI, with Vite proxying requests
during development. FastAPI exposes typed dashboard endpoints under `/api` and
retains the existing synchronous `/diagnose` contract.

A shared SQLite control store holds repository profiles, immutable configuration
revisions, submissions, activity evidence, and reports. The API persists each
submission before starting its stable Temporal workflow ID. A reconciler retries
dispatch after outages and recovers progress and terminal states after API restarts.
Temporal rejects duplicate starts, including lost-acknowledgement retries.

Dashboard workflows send only incident IDs and stage names to activities. Each
activity resolves the captured settings revision directly from the shared store;
LLM credentials never enter workflow histories. Evidence and actual activity timings
are persisted at stage boundaries. The workflow also exposes a read-only progress
query. The API uses Temporal's close timestamp for final run durations.

Every dashboard run uses a separate repository index. Index snapshots replace their
contents atomically, and generated dependency/build directories are excluded from
source discovery. CPU-bound indexing and source analysis execute off the async loop.

Saved configuration overrides environment/default values. Analysis and model settings
are captured for each new run. Infrastructure settings activate on process startup
only when there are no active incidents. The control-store bootstrap location stays
independent of artifact storage, and a transactional guard prevents an old API process
from submitting work after another process has activated different infrastructure.
