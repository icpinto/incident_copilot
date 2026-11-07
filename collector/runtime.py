"""Passive runtime worker, supervised by Vector. Stdout contains only result events."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import os
import re
import struct
import sys
import threading
import time
from collections import OrderedDict, deque
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from urllib.parse import urlencode

from runtime_transport import ReadFailure, docker_get, http_request

CAPABILITIES = ["logs", "resources", "dependencies", "traces", "latency"]
SECRET = re.compile(
    r'(?im)([\w.-]*(?:password|passwd|secret|api[_-]?key|token|authorization)[\w.-]*["\x27]?\s*[:=]\s*)[^\n,}]*'
)
URL_CREDENTIAL = re.compile(r"(\w+://)[^\s/:@]+:[^\s/@]+@")
TRACE = re.compile(r'\b(?:trace_id|traceId)\s*[=:]\s*["\x27]?([\w.:-]+)')


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def stamp(value):
    # Docker may supply nanoseconds; datetime accepts them and retains microseconds.
    return datetime.fromisoformat(value)


def trace_identity(value):
    if re.fullmatch(r"[a-fA-F0-9]{16,32}", value):
        return value.lower().zfill(32)
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        return None
    return decoded.hex() if len(decoded) == 16 else None


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {
            key: "[redacted]"
            if any(
                word in key.lower()
                for word in ("password", "secret", "authorization", "token", "api_key")
            )
            else redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if not isinstance(value, str):
        return value
    for secret in secrets:
        if secret and len(secret) >= 4:
            value = value.replace(secret, "[redacted]")
    value = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        "[redacted private key]",
        value,
        flags=re.DOTALL,
    )
    return SECRET.sub(r"\1[redacted]", URL_CREDENTIAL.sub(r"\1[redacted]@", value))


def docker_log_records(raw, tty=False):
    """Decode complete multiplex frames before splitting physical log lines."""
    streams = {1: bytearray(), 2: bytearray()}
    if tty:
        streams[1].extend(raw)
    else:
        offset = 0
        while offset < len(raw):
            if (
                offset + 8 > len(raw)
                or raw[offset] not in (1, 2)
                or raw[offset + 1 : offset + 4] != b"\0\0\0"
            ):
                raise ReadFailure("invalid_docker_log_frame")
            channel = raw[offset]
            length = struct.unpack(">I", raw[offset + 4 : offset + 8])[0]
            offset += 8
            if offset + length > len(raw):
                raise ReadFailure("incomplete_docker_log_frame")
            streams[channel].extend(raw[offset : offset + length])
            offset += length
    rows = []
    for channel, content in streams.items():
        text = content.decode("utf-8", "replace")
        # PEM bodies can span separately timestamped Docker records, including a clipped tail.
        text = re.sub(
            r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)",
            lambda match: "[redacted private key]" + "\n" * match[0].count("\n"),
            text,
            flags=re.DOTALL,
        )
        for line in text.splitlines():
            prefix, _, message = line.partition(" ")
            try:
                timestamp = stamp(prefix).isoformat()
            except ValueError:
                timestamp, message = None, line
            row = {
                "timestamp": timestamp,
                "stream": "stdout" if channel == 1 else "stderr",
                "message": message,
            }
            try:
                parsed = json.loads(message)
                if isinstance(parsed, dict):
                    row["message"] = str(parsed.get("message", parsed.get("msg", message)))
                    for key, aliases in {
                        "trace_id": ("trace_id", "traceId"),
                        "request_id": ("request_id", "requestId"),
                        "duration_ms": ("duration_ms",),
                        "level": ("level", "severity"),
                    }.items():
                        for alias in aliases:
                            if alias in parsed and isinstance(parsed[alias], (str, int, float)):
                                row[key] = parsed[alias]
                                break
            except (ValueError, TypeError):
                pass
            if "trace_id" not in row:
                match = TRACE.search(message)
                if match:
                    row["trace_id"] = match[1]
            rows.append(row)
    ordered = sorted(rows, key=lambda row: row["timestamp"] or "")
    ownership = {}
    for row in ordered:
        stream = row["stream"]
        if row.get("trace_id") or row.get("request_id"):
            ownership[stream] = {
                key: row[key] for key in ("trace_id", "request_id") if row.get(key)
            }
        elif row["message"].startswith(
            (" ", "\t", "Caused by:", "During handling", "The above exception")
        ) or re.match(r"^[\w.]+(?:Error|Exception):", row["message"]):
            row.update(ownership.get(stream, {}))
        else:
            ownership.pop(stream, None)
    return ordered


def normalize_stats(raw, previous=None):
    cpu = raw.get("cpu_stats", {})
    old = (previous or {}).get("cpu_stats", raw.get("precpu_stats", {}))
    current_total = cpu.get("cpu_usage", {}).get("total_usage", 0)
    cpu_delta = current_total - old.get("cpu_usage", {}).get("total_usage", 0)
    system_delta = cpu.get("system_cpu_usage", 0) - old.get("system_cpu_usage", 0)
    reset = cpu_delta < 0 or system_delta < 0
    count = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage", [])) or 1
    memory = raw.get("memory_stats", {})
    cache = memory.get("stats", {}).get(
        "inactive_file", memory.get("stats", {}).get("total_inactive_file", 0)
    )
    usage = max(0, memory.get("usage", 0) - cache)
    networks = list(raw.get("networks", {}).values())[:64]
    io = raw.get("blkio_stats", {}).get("io_service_bytes_recursive") or []
    return {
        "timestamp": raw.get("read") or datetime.now(UTC).isoformat(),
        "cpu_percent": cpu_delta / system_delta * count * 100
        if old.get("system_cpu_usage") is not None and system_delta > 0 and not reset
        else None,
        "memory_bytes": usage,
        "memory_limit_bytes": memory.get("limit"),
        "throttled_periods": cpu.get("throttling_data", {}).get("throttled_periods"),
        "network_rx_bytes": sum(item.get("rx_bytes", 0) for item in networks),
        "network_tx_bytes": sum(item.get("tx_bytes", 0) for item in networks),
        "block_read_bytes": sum(
            item.get("value", 0) for item in io if item.get("op", "").lower() == "read"
        ),
        "block_write_bytes": sum(
            item.get("value", 0) for item in io if item.get("op", "").lower() == "write"
        ),
        "counter_reset": reset,
    }


def resource_observation(service, container_id, samples):
    cpu = [point for point in samples if point.get("cpu_percent") is not None]
    return {
        "service": service,
        "container_id": container_id,
        "samples": samples,
        "summary": {
            "sample_count": len(samples),
            "peak_cpu": max(cpu, key=lambda point: point["cpu_percent"], default=None),
            "peak_memory": max(samples, key=lambda point: point["memory_bytes"], default=None),
            "counter_resets": sum(bool(point.get("counter_reset")) for point in samples),
        },
    }


class ResourceBuffer:
    def __init__(self, max_containers=128, max_bytes=32 * 1024 * 1024):
        self.samples, self.previous = OrderedDict(), {}
        self.max_containers, self.max_bytes = max_containers, max_bytes
        self.lock = threading.Lock()
        self.omitted = 0

    def add(self, container_id, raw):
        with self.lock:
            point = normalize_stats(raw, self.previous.get(container_id))
            observed = stamp(point["timestamp"])
            if observed.tzinfo is None or (
                point["cpu_percent"] is not None and not math.isfinite(point["cpu_percent"])
            ):
                raise ValueError("Invalid resource sample")
            point["timestamp"] = observed.astimezone(UTC).isoformat()
            cpu = raw.get("cpu_stats", {})
            self.previous[container_id] = {
                "cpu_stats": {
                    "cpu_usage": {"total_usage": cpu.get("cpu_usage", {}).get("total_usage", 0)},
                    "system_cpu_usage": cpu.get("system_cpu_usage", 0),
                }
            }
            queue = self.samples.setdefault(container_id, deque(maxlen=120))
            if queue and any(
                point.get(key, 0) < queue[-1].get(key, 0)
                for key in (
                    "network_rx_bytes",
                    "network_tx_bytes",
                    "block_read_bytes",
                    "block_write_bytes",
                )
            ):
                point["counter_reset"] = True
            queue.append(point)
            self.samples.move_to_end(container_id)
            cutoff = datetime.now(UTC) - timedelta(minutes=30)
            for key in list(self.samples):
                while self.samples[key] and stamp(self.samples[key][0]["timestamp"]) < cutoff:
                    self.samples[key].popleft()
                if not self.samples[key]:
                    self.samples.pop(key)
                    self.previous.pop(key, None)
            while (
                len(self.samples) > self.max_containers
                or len(encode(self.samples_as_dict()).encode()) > self.max_bytes
            ):
                key, _ = self.samples.popitem(last=False)
                self.previous.pop(key, None)
                self.omitted += 1

    def samples_as_dict(self):
        return {key: list(value) for key, value in self.samples.items()}

    def read(self, container_id, start, end):
        with self.lock:
            return [
                dict(item)
                for item in self.samples.get(container_id, [])
                if start <= stamp(item["timestamp"]) <= end
            ]


class RuntimeEngine:
    def __init__(self, config=None, docker=docker_get, http=http_request):
        self.config = config or {"profiles": {}}
        self.docker, self.http = docker, http
        self.buffer = ResourceBuffer()
        self.budgets, self.budget_lock = {}, threading.Lock()
        self.secrets = [
            value
            for name, value in os.environ.items()
            if any(word in name.lower() for word in ("token", "password", "secret", "api_key"))
        ]

    def token(self, profile, backend):
        settings = profile.get(backend, {})
        if settings.get("token_file"):
            token = Path(settings["token_file"]).read_text().strip()
        else:
            token = os.environ.get(settings.get("token_env", ""), "")
        if token and token not in self.secrets:
            self.secrets.append(token)
        return token

    def sample(self, policies):
        allowed = {row["project"] for row in policies}
        if not allowed:
            return
        deadline = time.time() + 10
        # Scope the Docker listing itself, rather than fetching every host container.
        filters = {"label": ["com.docker.compose.project"]}
        containers = self.docker(
            "/containers/json?" + urlencode({"filters": encode(filters)}), deadline=deadline
        )
        selected = [
            c
            for c in containers
            if (c.get("Labels") or {}).get("com.docker.compose.project") in allowed
        ]
        self.buffer.omitted = max(self.buffer.omitted, max(0, len(selected) - 128))
        for container in selected[:128]:
            if time.time() >= deadline:
                self.buffer.omitted += 1
                break
            try:
                raw = self.docker(
                    f"/containers/{container['Id']}/stats?stream=false&one-shot=true",
                    deadline=deadline,
                )
                self.buffer.add(container["Id"], raw)
            except (ReadFailure, ValueError, KeyError, TypeError, OverflowError):
                self.buffer.omitted += 1

    def execute(self, job):
        started = time.monotonic()
        scope, query = job["scope"], job["query"]
        end = stamp(scope["anchor"])
        start = end - timedelta(minutes=query["lookback_minutes"])
        deadline = min(stamp(job["deadline"]).timestamp(), time.time() + 14)
        service = query.get("service") or scope["service"]
        limitations = list(scope.get("limitations", []))
        failures = []
        provenance = {
            "collector_id": scope["collector_id"],
            "project": scope["project"],
            "environment": scope["environment"],
            "service": service,
            "container_ids": [],
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "observed_at": datetime.now(UTC).isoformat(),
            "coverage": "partial",
        }
        result = {
            "capability": query["capability"],
            "status": "ok",
            "reason": None,
            "provenance": provenance,
            "data": {},
            "limitations": limitations,
            "truncated": False,
            "duration_ms": 0,
        }
        budget_key = job.get("incident_id", job["id"]) if job.get("baseline") else job["id"]
        request_reads = 0

        def reserve():
            nonlocal request_reads
            with self.budget_lock:
                now = time.time()
                self.budgets = {key: val for key, val in self.budgets.items() if val[1] > now}
                count, expiry = self.budgets.get(budget_key, (0, now + 60))
                if count >= 30 or request_reads >= job.get("max_reads", 30) or now >= deadline:
                    raise ReadFailure("backend_read_budget")
                request_reads += 1
                self.budgets[budget_key] = (count + 1, expiry)

        def get(path, raw=False):
            reserve()
            return self.docker(path, deadline=deadline, raw=raw)

        def fetch(*args, **kwargs):
            reserve()
            return self.http(*args, **kwargs)

        try:
            if service not in scope["services"] or not 1 <= query["lookback_minutes"] <= 15:
                raise ReadFailure("invalid_runtime_scope")
            if query.get("trace_id") and query["trace_id"] not in scope["trace_ids"]:
                raise ReadFailure("unobserved_trace_id")
            profile = self.config.get("profiles", {}).get(scope.get("telemetry_profile"), {})
            if profile and (
                profile.get("project") != scope["project"]
                or profile.get("environment") != scope["environment"]
            ):
                raise ReadFailure("telemetry_profile_scope_mismatch")
            capability = query["capability"]
            if capability == "traces":
                result["data"] = self.traces(scope, query, profile, start, end, deadline, fetch)
            elif capability == "latency":
                result["data"] = self.metrics(scope, service, profile, start, end, deadline, fetch)
            else:
                services = [service] if query.get("service") else scope["services"]
                pairs = [
                    (name, cid) for name in services for cid in scope["containers"].get(name, [])
                ][:10]
                if not pairs:
                    raise ReadFailure("container_unavailable")
                rows = []
                for name, cid in pairs:
                    try:
                        inspected = get(f"/containers/{cid}/json")
                        labels = inspected.get("Config", {}).get("Labels") or {}
                        if (
                            inspected.get("Id") != cid
                            or labels.get("com.docker.compose.project") != scope["project"]
                            or labels.get("com.docker.compose.service") != name
                        ):
                            raise ReadFailure("container_identity_mismatch")
                        provenance["container_ids"].append(cid)
                        if capability == "dependencies":
                            state = inspected.get("State", {})
                            rows.append(
                                {
                                    "service": name,
                                    "container_id": cid,
                                    "state": state.get("Status"),
                                    "oom_killed": state.get("OOMKilled"),
                                    "exit_code": state.get("ExitCode"),
                                    "started_at": state.get("StartedAt"),
                                    "finished_at": state.get("FinishedAt"),
                                    "restart_count": inspected.get("RestartCount"),
                                    "health": state.get("Health", {}).get(
                                        "Status", "not_configured"
                                    ),
                                    "failing_streak": state.get("Health", {}).get("FailingStreak"),
                                }
                            )
                        elif capability == "resources":
                            samples = self.buffer.read(cid, start, end)
                            rows.append(resource_observation(name, cid, samples))
                            if not samples or stamp(samples[0]["timestamp"]) > start + timedelta(
                                seconds=30
                            ):
                                limitations.append(
                                    "Resource history incomplete: buffer warm-up, eviction, or expired history."
                                )
                            if any(
                                stamp(b["timestamp"]) - stamp(a["timestamp"])
                                > timedelta(seconds=30)
                                for a, b in pairwise(samples)
                            ):
                                limitations.append("Resource sampling gaps exceed 30 seconds.")
                        elif capability == "logs":
                            params = {
                                "stdout": "true",
                                "stderr": "true",
                                "timestamps": "true",
                                "follow": "false",
                                "since": start.timestamp(),
                                "until": end.timestamp(),
                                "tail": 200,
                            }
                            raw = get(f"/containers/{cid}/logs?" + urlencode(params), raw=True)
                            logs = docker_log_records(
                                raw, inspected.get("Config", {}).get("Tty", False)
                            )
                            excluded = 0
                            for log in logs:
                                if (
                                    log.get("trace_id")
                                    and log["trace_id"] not in scope["trace_ids"]
                                ) or (
                                    log.get("request_id")
                                    and log["request_id"] not in scope.get("request_ids", [])
                                    and log.get("trace_id") not in scope["trace_ids"]
                                ):
                                    excluded += 1
                                    continue
                                if log["timestamp"] and not start <= stamp(log["timestamp"]) <= end:
                                    continue
                                rows.append(
                                    log
                                    | {
                                        "service": name,
                                        "container_id": cid,
                                        "association": "trace"
                                        if log.get("trace_id")
                                        else "context_only",
                                    }
                                )
                            if excluded:
                                limitations.append(
                                    f"Excluded {excluded} log record(s) from unrelated traces."
                                )
                            if len(logs) >= 200:
                                result["truncated"] = True
                                limitations.append(
                                    "Docker log tail limited to 200 physical lines per container."
                                )
                    except (ReadFailure, ValueError, KeyError) as error:
                        if (
                            capability == "resources"
                            and isinstance(error, ReadFailure)
                            and str(error) != "container_identity_mismatch"
                        ):
                            samples = self.buffer.read(cid, start, end)
                            if samples:
                                rows.append(resource_observation(name, cid, samples))
                                provenance["container_ids"].append(cid)
                        limitations.append(
                            f"{name}: {str(error) if isinstance(error, ReadFailure) else 'invalid_container_response'}"
                        )
                        failures.append(
                            str(error)
                            if isinstance(error, ReadFailure)
                            else "invalid_container_response"
                        )
                result["data"] = {"observations": rows}
                if capability == "dependencies":
                    provenance["coverage"] = "snapshot"
                    limitations.append(
                        "Health and exit state observed at collection time; not proof of historical health or causation."
                    )
                    if profile.get("prometheus"):
                        metrics = []
                        for name in services:
                            try:
                                metrics.extend(
                                    self.metrics(
                                        scope,
                                        name,
                                        profile,
                                        start,
                                        end,
                                        deadline,
                                        fetch,
                                        presets={"availability", "saturation"},
                                    )["metrics"]
                                )
                            except ReadFailure as error:
                                limitations.append(f"{name} metrics: {error}")
                        result["data"]["metrics"] = metrics
                if capability == "resources":
                    limitations.append(
                        "Network counters measure traffic, not connectivity or dependency health."
                    )
                    if self.buffer.omitted:
                        limitations.append("Collector resource sampling coverage is incomplete.")
                times = [row.get("timestamp") for row in rows if row.get("timestamp")]
                times += [p["timestamp"] for row in rows for p in row.get("samples", [])]
                if times:
                    provenance.update(actual_start=min(times), actual_end=max(times))
            if capability == "traces":
                spans = result["data"].get("spans", [])
                if spans:
                    times = [
                        datetime.fromtimestamp(span["start_unix_ns"] / 1e9, UTC).isoformat()
                        for span in spans
                    ]
                    provenance.update(actual_start=min(times), actual_end=max(times))
                result["truncated"] = bool(result["data"].get("span_limit_reached"))
                limitations.append(
                    "Traces are sampled and restricted to mapped services; missing spans do not prove absence."
                )
            if capability == "latency":
                times = [
                    datetime.fromtimestamp(point[0], UTC).isoformat()
                    for metric in result["data"].get("metrics", [])
                    for point in metric["values"]
                ]
                if times:
                    provenance.update(actual_start=min(times), actual_end=max(times))
            observations = result["data"].get("observations", [])
            useful = (
                any(row.get("samples") for row in observations)
                if capability == "resources"
                else bool(observations)
            )
            useful = (
                useful
                or bool(result["data"].get("spans"))
                or any(metric.get("values") for metric in result["data"].get("metrics", []))
            )
            if not useful:
                result["status"] = "unavailable" if failures else "empty"
                result["reason"] = "; ".join(dict.fromkeys(failures))[:512] or None
            elif limitations:
                result["status"] = "partial"
        except (
            ReadFailure,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            OverflowError,
            RecursionError,
        ) as error:
            result.update(
                status="unavailable",
                reason=str(error) if isinstance(error, ReadFailure) else "invalid_runtime_response",
            )
            provenance["coverage"] = "unavailable"
        result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
        return self.bound(redact(result, self.secrets))

    def traces(self, scope, query, profile, start, end, deadline, fetch):
        backend = profile.get("tempo")
        if not backend:
            raise ReadFailure("tempo_not_configured")
        ids = [query["trace_id"]] if query.get("trace_id") else scope["trace_ids"][:2]
        if not ids:
            raise ReadFailure("no_observed_trace_id")
        aliases = profile.get("service_aliases", {})
        spans, allowed = [], list(scope["services"])
        for trace_id in ids:
            if not re.fullmatch(r"[A-Fa-f0-9]{16,32}", trace_id):
                raise ReadFailure("unsupported_trace_id")
            raw = fetch(
                backend["url"],
                f"/api/traces/{trace_id}?"
                + urlencode({"start": int(start.timestamp()), "end": int(end.timestamp())}),
                token=self.token(profile, "tempo"),
                deadline=deadline,
            )
            for batch in raw.get("batches", raw.get("resourceSpans", [])):
                attributes = {
                    a.get("key"): a.get("value", {}).get("stringValue")
                    for a in batch.get("resource", {}).get("attributes", [])
                }
                service = aliases.get(attributes.get("service.name"))
                if service not in scope["containers"]:
                    continue
                if service not in allowed and len(allowed) < 5:
                    allowed.append(service)
                if service not in allowed:
                    continue
                for group in batch.get("scopeSpans", batch.get("instrumentationLibrarySpans", [])):
                    for span in group.get("spans", []):
                        if trace_identity(span.get("traceId", "")) != trace_identity(trace_id):
                            continue
                        begin, finish = (
                            int(span.get("startTimeUnixNano", 0)),
                            int(span.get("endTimeUnixNano", 0)),
                        )
                        if (
                            not int(start.timestamp() * 1e9) <= begin <= int(end.timestamp() * 1e9)
                            or finish < begin
                        ):
                            continue
                        spans.append(
                            {
                                "trace_id": trace_id,
                                "span_id": span.get("spanId"),
                                "parent_span_id": span.get("parentSpanId"),
                                "service": service,
                                "name": str(span.get("name", ""))[:200],
                                "start_unix_ns": begin,
                                "duration_ms": max(0, finish - begin) / 1_000_000,
                                "error": span.get("status", {}).get("code")
                                in {2, "STATUS_CODE_ERROR"},
                            }
                        )
                        if len(spans) >= 200:
                            return {"spans": spans, "span_limit_reached": True}
        return {
            "spans": spans,
            "sampled": True,
            "scope": "Mapped service spans only; missing spans do not prove absence.",
        }

    def metrics(self, scope, service, profile, start, end, deadline, fetch, presets=None):
        backend = profile.get("prometheus")
        if not backend:
            raise ReadFailure("prometheus_not_configured")
        bindings = profile.get("metric_bindings", {}).get(service)
        if not bindings:
            raise ReadFailure("metric_service_not_mapped")
        # Operator-owned templates use a single literal selector placeholder. No model text enters PromQL.
        matchers = {**bindings, **profile.get("scope_labels", {})}
        if not {scope["project"], scope["environment"], scope["collector_id"]} <= set(
            profile.get("scope_labels", {}).values()
        ):
            raise ReadFailure("metric_scope_labels_required")
        if any(
            not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", key) or not isinstance(value, str)
            for key, value in matchers.items()
        ):
            raise ReadFailure("invalid_metric_bindings")
        selector = ",".join(
            f"{key}={json.dumps(value)}"
            for key, value in sorted(matchers.items())
            if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", key) and isinstance(value, str)
        )
        observations = []
        templates = backend.get("templates", {})
        if (
            presets is None
            and any(name.startswith("latency_p") for name in templates)
            and "request_count" not in templates
        ):
            raise ReadFailure("latency_request_count_required")
        for preset, template in list(backend.get("templates", {}).items())[:5]:
            if presets is not None and preset not in presets:
                continue
            if preset not in {
                "request_count",
                "error_rate",
                "latency_p50",
                "latency_p95",
                "latency_p99",
                "availability",
                "saturation",
            }:
                continue
            if "{scope}" not in template:
                raise ReadFailure("unscoped_metric_template")
            query = template.replace("{scope}", selector)
            params = {
                "query": query,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": 15,
                "timeout": "5s",
                "limit": 20,
            }
            raw = fetch(
                backend["url"],
                "/api/v1/query_range?" + urlencode(params),
                token=self.token(profile, "prometheus"),
                deadline=deadline,
            )
            if raw.get("status") != "success":
                raise ReadFailure("prometheus_query_failed")
            for series in raw.get("data", {}).get("result", [])[:20]:
                # Do not export arbitrary labels which may contain tenant data or secrets.
                values = [
                    [float(t), float(v)]
                    for t, v in series.get("values", [])[:61]
                    if math.isfinite(float(v)) and start.timestamp() <= float(t) <= end.timestamp()
                ]
                observations.append({"service": service, "preset": preset, "values": values})
        return {
            "metrics": observations,
            "coverage": "Configured histogram quantiles require request_count; samples are not population guarantees.",
        }

    @staticmethod
    def bound(result):
        result["limitations"] = list(dict.fromkeys(result["limitations"]))[:30]
        while len(encode(result).encode()) > 7400:
            result["truncated"] = True
            result["status"] = "partial"
            result["provenance"]["coverage"] = "partial"
            if "Runtime response clipped to evidence byte budget." not in result["limitations"]:
                result["limitations"].append("Runtime response clipped to evidence byte budget.")
            arrays = [
                value for value in result["data"].values() if isinstance(value, list) and value
            ]
            if arrays:
                values = max(arrays, key=lambda value: len(encode(value)))
                nested = [
                    entry[key]
                    for entry in values
                    if isinstance(entry, dict)
                    for key in ("samples", "values")
                    if isinstance(entry.get(key), list) and len(entry[key]) > 2
                ]
                if nested:
                    points = max(nested, key=len)
                    points[:] = points[::2] + (
                        [points[-1]] if points[-1] not in points[::2] else []
                    )
                else:
                    values.pop()
            elif result["data"]:
                result["data"] = {}
            elif len(result["limitations"]) > 1:
                result["limitations"].pop(0)
            else:
                break
        return result


async def main():
    from start import collector_identity

    identity = os.environ.get("IC_COLLECTOR_ID") or collector_identity(Path("/var/lib/vector"))
    config = json.loads(
        Path(os.environ.get("IC_RUNTIME_CONFIG", "/etc/vector/runtime.json")).read_text()
    )
    engine = RuntimeEngine(config)
    endpoint, token = (
        os.environ.get("IC_COLLECTOR_API_URL", "http://api:8080"),
        os.environ["IC_LOG_INGEST_TOKEN"],
    )
    policies, jobs, last_sample, policy_at = [], set(), 0, 0

    async def execute(job):
        result = await asyncio.to_thread(engine.execute, job)
        print(
            encode(
                {
                    "collector_id": identity,
                    "request_id": job["id"],
                    "lease": job["lease"],
                    "result": result,
                }
            ),
            flush=True,
        )

    async def sample():
        try:
            await asyncio.to_thread(engine.sample, policies)
        except (ReadFailure, ValueError, OSError):
            engine.buffer.omitted += 1

    sampler = None
    while True:
        jobs = {task for task in jobs if not task.done()}
        try:
            reply = await asyncio.to_thread(
                http_request,
                endpoint,
                "/api/collector/runtime/claim",
                token=token,
                body={
                    "collector_id": identity,
                    "capabilities": CAPABILITIES,
                    "helper": {
                        "status": "running",
                        "sampled_containers": len(engine.buffer.samples),
                        "sampling_omissions": engine.buffer.omitted,
                    },
                },
            )
            policies, policy_at = reply["policies"], time.time()
            for job in reply["jobs"][: max(0, 2 - len(jobs))]:
                jobs.add(asyncio.create_task(execute(job)))
        except (ReadFailure, ValueError, KeyError, OSError):
            print("Runtime control connection unavailable; retrying.", file=sys.stderr, flush=True)
        if time.time() - policy_at > 60:
            policies = []
        if time.time() - last_sample >= 15 and (sampler is None or sampler.done()):
            sampler = asyncio.create_task(sample())
            last_sample = time.time()
        await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
