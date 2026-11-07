# Connect incidents to deployed versions

Version linking is opt-in for each Compose service. When enabled, an incident uses
an isolated checkout of the commit declared by its emitting container, even if the
repository profile has synced a newer branch revision. GitHub and local Git
profiles are supported. Image tags, branch names, and arrival time are never used
to guess a source commit.

## Label application images

Build your image from a clean, committed source tree. Set these labels in the
**final image stage**, or pass their equivalents with `docker build --label`:

```dockerfile
ARG GIT_SHA
ARG SOURCE_URL
ARG RELEASE_VERSION
LABEL org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.version="${RELEASE_VERSION}"
```

Example in PowerShell:

```powershell
$releaseCommit = git rev-parse HEAD
docker build --build-arg GIT_SHA=$releaseCommit --build-arg SOURCE_URL=https://github.com/acme/commerce --build-arg RELEASE_VERSION=v1.2.0 -t commerce:v1.2.0 .
```

`revision` must be a complete 40-character SHA-1 or 64-character SHA-256 Git commit
ID. Short SHAs and branch names are rejected. Version and source URL are optional;
when provided for a GitHub profile, the source URL must match the configured
HTTPS github.com repository. Labels cannot select a different repository or path.
Do not place credentials in labels. Push the commit before investigating a GitHub
release. Local profiles must already contain the commit in their Git history.

Recreate application containers after rebuilding. Existing containers retain their
original effective labels. For images built with multiple stages, place these
labels in the final stage. See the [OCI annotation definitions](https://github.com/opencontainers/image-spec/blob/main/annotations.md).

Labels declare build provenance. They do not verify image contents, build inputs,
runtime bind mounts, or incident causation. Runtime changes can differ from the
packaged source even when the declared commit matches.

## Enable collection and linking

1. Upgrade and restart API and worker first. The database migration is additive;
   existing sources keep version linking disabled and environment `unknown`.
2. Rebuild and recreate the standalone collector using
   `docker compose -f docker-compose.collector.yml up --build -d`.
3. In **Log sources**, map the collector/project to a repository, enter an
   environment such as `production`, select application services under **Analyze
   deployed versions**, and save. Leave third-party services unselected unless
   their source belongs to the mapped repository.
4. Inspect **Observed container versions**. New errors from selected services pin
   the emitting container's declared commit. A prior GitHub branch sync is not
   needed for version-linked analysis.

Each collector generates a UUID in its existing `/var/lib/vector` volume. Keep the
volume across restarts. Set `IC_COLLECTOR_ID` to override the identity with a stable,
unique value per Docker host. Reusing an identity across hosts can confuse container
observations. Old buffered payloads and old collector versions use the reserved
`legacy` identity, preserving their existing mappings and retry deduplication.
The upgraded collector appears as a new source; configure its mapping explicitly.

The collector reports image references, local image IDs, and available registry
RepoDigests. A local image ID identifies its configuration object; it is not a
registry manifest digest. Locally built images may have no registry digest. Missing
values display as unknown. Image inspection failures do not discard error logs.

The collector also inspects each observed container's effective environment and
mount metadata. It sends at most 128 environment names and 64 mount destinations,
types, and access modes. Only a short allowlist of non-secret settings (environment,
log level, debug, port, host, worker count, and feature flags) includes values;
other values are recorded as `[redacted]`. Host mount sources and mounted file
contents are never sent. The incident freezes the most recent observation for its
container at submission, and both analysis paths receive a bounded view of it.
If the first log arrives before inventory or inspection fails, configuration may
be unavailable for that incident. Rebuild the collector to enable capture.

Container observations remain after containers disappear. Their timestamps mean
first and last **observed**, not the exact deployment or termination time. Buffered
log batches retain their container and release declaration. They are never
reassigned to a newer running release. Environment and repository mappings are
captured at ingestion/submission; subsequent edits do not rewrite saved incidents.

## Investigating and retrying

Incident details show declared and analyzed commits separately, plus environment,
release version, image identity, and source resolution:

- `pending`: the worker has not prepared the requested source yet.
- `matched`: the isolated source matches the declared commit.
- `unavailable`: metadata, source access, or preparation prevented analysis.
- `unknown`: version linking is disabled or this is a historical incident.

A missing/invalid revision or unavailable commit preserves the incident and error
occurrence, but stops source analysis. Correct missing image labels and recreate
the container for new errors. For a known commit, fix repository access or make the
commit available, then use **Rerun**. Reruns retain the original deployment and
commit; they do not switch to the latest release. Transient Git connectivity errors
receive the existing bounded activity retries.

Local profiles must point at the Git repository root. Their checkout may be dirty
or ahead of the deployed commit: only committed objects are imported into managed
storage. The original checkout is not modified. An absolute local Compose path is
supported only when it lies inside the configured repository. As with normal
GitHub snapshots, submodules and Git LFS downloads are not expanded.

**New analysis** can explicitly select an observed deployment for pasted logs.
Choose the container that produced the logs, including an older container when
necessary. The deployment must map to the selected repository and agree with any
service hint. Leaving this selection empty preserves the configured-source flow.

Every investigation uses one selected service revision. Mixed-version service
investigations, configuration diffs, Kubernetes, and CI/CD
deployment-event ingestion are outside this release.

## API changes and counting

- `GET /api/deployments`: `repository_id`, `service`, `environment`, `collector_id`,
  and `project` filters; `page` starts at 1 and `page_size` is 1-100 (default 20).
- `PUT /api/log-sources/{project}?collector_id=...`: adds `environment` and
  `versioned_services`. Omitting collector ID addresses the legacy source.
- `POST /api/incidents`: optional `deployment_id`; source preparation remains
  asynchronous and submissions return `202`.
- Incident responses and completed JSON/Markdown reports include
  `deployment_context` and `source_resolution`. Error occurrence events also retain
  their deployment context, even when an investigation is suppressed by cooldown.
- Collector inventory adds `collector_id` and `containers`; each container includes
  `project`, `service`, `container_id`, and a `release` object containing `revision`,
  `version`, `source_url`, `image_ref`, `image_id`, and `repo_digests`, plus a bounded
  `runtime_config` observation. Log batches add
  the same collector/release metadata. All additions are optional for old clients.

Issue fingerprints remain stable across releases. Investigation cooldowns include
collector/project, environment, service, and release/artifact identity. Later image
metadata enrichment does not restart a cooldown. Collector retry deduplication is
scoped by collector identity; occurrence capture and investigation creation commit
atomically. Identical pasted logs selected against different deployments are
separate uploads; identical uploads against the same deployment and reruns share
occurrences.

Managed source snapshots, indexes, graph storage, and the control database must
remain accessible at the same paths in API and worker, as in existing repository
sync. Back them up together. Published snapshots remain retained for reproducible
reruns; this release does not add cache pruning.

## Verification

Run the automated checks:

```powershell
python -m pytest -q
$env:IC_RUN_TEMPORAL_TESTS = '1'
python -m pytest tests/test_temporal_integration.py tests/test_deployed_versions.py -q
cd apps/web
npm test
npm run build
```

For a live smoke test with Docker Engine and the application stack running:

1. Commit application version A and record its SHA. Build it with the revision label
   and run it in a test Compose project. Enable version linking for its service.
2. Commit version B, leaving the source checkout at B. For a GitHub profile, push
   both commits and sync the profile to B. Keep the running container at A.
3. Emit an exception with a source location that differs between A and B. Confirm
   the saved incident shows A as both declared and analyzed commit, and source
   evidence contains A's code. The repository profile must remain at B.
4. Rebuild/recreate the container at B and emit the same error within one hour.
   Confirm a new investigation links to B while the recurring issue stays grouped.
5. Retry a buffered batch from A. Confirm it remains linked to A and is counted once.
6. Start a container without a revision label. Confirm its error is captured and
   source analysis fails with the missing-label explanation.
