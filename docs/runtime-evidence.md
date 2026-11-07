# Runtime evidence

The standalone collector automatically captures passive runtime evidence for new
deployment-linked incidents from enabled Log sources. It keeps Docker access inside the
collector; API and worker containers do not need the Docker socket. Historical incidents
retain their saved evidence without runtime retrieval.

In **Log sources**, enable runtime evidence, select up to four dependencies per failing
service, and optionally name a telemetry profile. Only configured dependencies and explicitly
aliased services appearing in an observed incident trace can extend the related-service scope.
Source linking still identifies the declared commit. If that source cannot be prepared, new
runtime-enabled incidents continue with source tools disabled and an explicit source gap.

## Collection and limits

The helper runs as a Vector streaming exec source and polls authenticated outbound work every
two seconds. Five automatic requests are committed with incident creation. Leases, results,
deadlines, and fixed incident scope survive API/worker restarts. The collector helper's metrics
buffer is in memory: restarting it loses that history and reports warm-up gaps.

| Limit | Value |
|---|---|
| Default / maximum lookback | 5 / 15 minutes, ending at the pinned incident timestamp |
| Related scope | 5 services / 10 containers |
| Backend HTTP read | 5 seconds and 1 MiB, no redirects or compressed responses |
| Automatic snapshot / follow-up deadline | 30 / 15 seconds |
| Concurrent jobs / automatic backend reads | 2 / 30 per incident |
| Resource sampling | Every 15 seconds, 30 minutes, 128 containers, 32 MiB |
| Retained capability response | 8 KiB, including provenance and clipping metadata |
| Investigator runtime follow-ups | 4 within the existing 12-request shared budget |
| Investigation evidence / model input | 64 / 32 KiB |

Inspect responses export only state, health status, exit/OOM information, and restart counts.
They never export environment variables, healthcheck commands/output, or complete inspect
documents. Inspect health is a snapshot at collection time, not a claim about incident-time
health. Resource samples contain cumulative network and I/O counters; those counters do not
measure connectivity. CPU can exceed 100% when a container uses multiple cores.

Logs retain timestamps, stream, message, trace/request IDs and explicit `duration_ms` when
present. Other trace IDs are excluded from diagnosis evidence. Unlinked dependency log lines
are contextual observations, not proof of a request path. Docker logging-driver failures,
removed original containers, missing samples, and unavailable telemetry remain explicit gaps.
Queries never replace the incident's original container with a new container of the same service.

## Optional telemetry profiles

Mount a JSON configuration with `IC_RUNTIME_CONFIG_PATH=/absolute/path/runtime.json` when
starting `docker-compose.collector.yml`. The default configuration contains no profiles.
The collector must be able to reach the configured backends on its Docker network. Export
application metrics/traces to your existing telemetry stack separately; the collector does not
instrument applications or deploy production telemetry infrastructure.

```json
{
  "profiles": {
    "payments-prod": {
      "project": "payments",
      "environment": "production",
      "service_aliases": {"payments-api": "api", "payments-db": "db"},
      "scope_labels": {"compose_project": "payments", "environment": "production", "collector_id": "payments-host"},
      "metric_bindings": {"api": {"service_name": "payments-api"}, "db": {"service_name": "payments-db"}},
      "prometheus": {
        "url": "http://prometheus:9090",
        "token_env": "PROMETHEUS_TOKEN",
        "templates": {
          "request_count": "sum(increase(http_requests_total{{scope}}[5m]))",
          "error_rate": "sum(rate(http_errors_total{{scope}}[5m]))",
          "latency_p95": "histogram_quantile(0.95,sum by(le)(rate(http_request_duration_seconds_bucket{{scope}}[5m])))"
        }
      },
      "tempo": {"url": "http://tempo:3200", "token_file": "/run/secrets/tempo-token"}
    }
  }
}
```

Names and metric templates are operator-owned, not model arguments. `{scope}` is replaced by
escaped exact label matchers. Scope-label values must include the pinned collector, project,
and environment, and the profile's project/environment must match the incident. Metric names
must match your exporters. Supported presets are `request_count`, `error_rate`, `latency_p50`,
`latency_p95`, `latency_p99`, `availability`, and `saturation`. Configure request counts alongside
histogram percentiles. No arbitrary query, URL, SQL, shell command, or probe is accepted from
the investigator. Backend bearer credentials stay in the collector environment or secret files.

Tempo retrieves up to two observed hexadecimal trace IDs per request, normalizing up to 200
spans with service aliases, parent relationships, durations, and error status. Unmapped service
spans and arbitrary span attributes are omitted. Sampled span durations and log durations are
individual observations, not service-wide latency percentiles.

## Recovery and interpretation

Disabling collection cancels pending work and prevents result acceptance. An expired lease may
be reclaimed without resetting the original deadline. Duplicate completed deliveries are
acknowledged without replacing evidence. Backend read allowances are reserved before leasing;
lost acknowledgements and helper restarts cannot reset the 30-read automatic snapshot budget.
Cancellation stops further requests. Completed reports
retain the evidence used for that diagnosis; reruns retain the original anchor and observations.

Reports show the suspected failure layer (`application`, `database`, `network`,
`infrastructure`, or `unknown`), citations, collection outcomes, provenance, and limitations.
These labels are provisional hypotheses. A healthy container or a timeout alone does not
establish a cause. Missing telemetry can still lead to `insufficient_evidence`.

The Log sources card shows helper freshness, capabilities, sampled-container count and sampling
omissions. Incident runtime collection shows query outcomes and durations. The same evidence and
limitations appear in JSON and Markdown exports. Deploy the API and worker before upgrading
collectors. In-flight historical incidents retain the original source-resolution behavior.

## Development fixture

`tests/fixtures/runtime/docker-compose.yml` provides a disposable application and dependency
with controlled failures. Its optional `telemetry` profile adds Prometheus and Tempo for local
verification. See its README before starting it. Normal tests use controlled transports and do
not require Docker, an external model, or a telemetry backend.
