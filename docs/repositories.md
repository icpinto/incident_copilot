# Repository sources and manual GitHub sync

Open **Repositories → Add repository** and choose **Local directory** or **GitHub**.
Existing profiles remain local profiles without reconfiguration.

## Local directory

Enter an existing absolute directory accessible to the worker. With Compose,
`IC_REPOSITORIES_DIR` is mounted read-only at `/repositories`; use that container
path or a checkout beneath it. Host workers use host paths such as `C:\Dev\commerce`.
The optional Compose file can be relative to the repository or an absolute worker
path. Use **Validate access**, then start an analysis as before. Each local incident
still creates its own index. Managed graph evidence requires a clean Git checkout.

## GitHub

Enter an HTTPS URL such as `https://github.com/owner/repository`, the exact tracked
branch, and an optional Compose path relative to the repository. Only GitHub.com
is supported. SSH URLs, embedded credentials, and escaping Compose paths are rejected.

Public repositories need no token. For private repositories, create a fine-grained
personal access token restricted to the selected repository with **Contents: read**
permission (Metadata read is included). Organizations may require approval. See
[GitHub's token documentation](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).
Tokens are stored in the private local control database, separately from profiles.
They are excluded from API responses, Temporal payloads, and Git URLs/configuration.
As with existing LLM credentials, filesystem permissions protect this database;
the application does not provide encryption at rest.

Saving does **not** contact GitHub. Click **Check & sync** to request a durable job
through the existing Temporal worker. It checks the branch, downloads changes,
creates a commit-specific checkout, builds its SQLite index, and prepares optional
graph evidence. Cards show progress, synced commit, and check/sync times.
Repeated clicks share the active job. Requested jobs resume when Temporal and a
matching worker reconnect. There is no schedule or automatic remote polling.

An unchanged commit with a valid prepared index skips downloading and rebuilding.
Check/fetch/indexing failures preserve the previous usable snapshot. Graph failures
are reported separately and normal diagnosis remains available. Fix expired tokens
or missing branches, then click the button again. Transient connectivity errors
receive bounded retries within the requested job.

To replace a token, edit the profile and enter its replacement. A blank field keeps
the stored token; **Remove saved GitHub token** deletes it. Changes to URL, branch,
Compose path, or source type require a new successful sync for GitHub analysis.
Previous incidents retain their original source snapshots.

## Cached analysis and reproducibility

The first successful sync enables analysis. New incidents use the last successfully
synced commit, including while GitHub is offline or another sync is in progress.
Startup, dashboard reads, graph refreshes, and analysis submissions do not check
GitHub. Dashboard status polling only reads local job records.

Each GitHub incident captures its source at submission and receives an isolated copy
of the prepared SQLite index. Reruns keep that original source revision. URL, branch,
and commit appear in incident details and JSON/Markdown exports. Submit a new analysis
after syncing to investigate newer source. Parser/index-format changes can rebuild
SQLite indexes locally without a remote check. Local directory behavior is unchanged.

## Deployment and storage

Rebuild and recreate the application and graph services:

```powershell
docker compose -f docker-compose.dev.yml up -d --build api worker gitnexus
```

Compose adds the persistent `repository-cache` volume at `/repository-cache` in all
three services: writable by the worker, read-only in API and graph. The existing
`/repositories` mount remains for local profiles. No incoming GitHub connection,
public endpoint, webhook, or tunnel is required.

For host deployments, `IC_REPOSITORY_CACHE_DIR` defaults to
`IC_DATA_DIR/repository-cache`. Git must be installed on the worker. A separate graph
process must see the entire cache, including its shared bare object stores, at the
**same absolute path** and allow it through `IC_REPOSITORY_CACHE_DIR`. Windows host
paths cannot be mounted into a Linux graph container as identical absolute paths;
use the full Compose deployment for managed GitHub graph evidence, or disable graph
evidence for that host setup.

Cache-path changes are infrastructure changes. Finish queued/running incidents and
syncs, then restart both API and worker. Custom container paths require matching
volume mounts and graph allowed-root configuration. Back up the control database,
artifact directories, repository cache, and graph volumes together with services stopped.

Published snapshots are retained in v1, including after profile edits or archival.
There is no automatic cache pruning; retained snapshots support reruns and consume
disk space. Submodule expansion, Git LFS downloads, arbitrary commit selection,
GitHub Enterprise, and automatic scheduled/webhook syncing are deferred.
