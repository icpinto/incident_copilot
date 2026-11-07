# Disposable runtime evidence fixture

Run `docker compose -f tests/fixtures/runtime/docker-compose.yml up -d` on a
development Docker host. Add `--profile telemetry` before `up` for optional
Prometheus (localhost:19090) and Tempo (localhost:13200).

Map the discovered `runtime-evidence-fixture` project to a repository, set the
environment to `test`, enable runtime evidence, and map `api` to dependency `db`.
For telemetry, use collector ID `runtime-fixture`; configure a profile matching
those labels, with backends reachable from the collector. On Docker Desktop use
`http://host.docker.internal:19090` and `http://host.docker.internal:13200`.
Map the Tempo alias `api` to Compose service `api`. The fixture intentionally
omits source revision labels so deployed-version linking exercises runtime-only
diagnosis when selected.

Send GET requests to these localhost endpoints:

- `http://localhost:18081/scenario/application`, then `/request`: application error.
- `http://localhost:18082/scenario/unhealthy`, then API `/request`: dependency error and failing existing healthcheck.
- Dependency `/scenario/slow`, then API `/request`: dependency latency and application timeout.
- Dependency `/scenario/oom`, then API `/request`: terminate only the disposable dependency through its 64 MiB memory limit.
- `/scenario/healthy` restores normal behavior; recreate the dependency after OOM.

Wait at least 30 seconds before a scenario to collect resource samples. Check
citations, time ranges, unavailable telemetry, clipped evidence, OOM state, and
source gaps. Optional metrics require multiple scrapes and sufficient requests.
A timeout scenario alone must not be treated as proof of a network failure.

Stop and remove only this fixture using
`docker compose -f tests/fixtures/runtime/docker-compose.yml down`.
