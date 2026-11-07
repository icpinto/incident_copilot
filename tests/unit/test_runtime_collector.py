import copy
import importlib
import json
import struct
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "collector"))
    # Avoid depending on another test module's dynamically loaded inventory/start modules.
    return importlib.import_module("runtime")


def job(capability="dependencies", **query):
    return {
        "id": "request",
        "incident_id": "incident",
        "lease": "lease",
        "baseline": True,
        "deadline": (datetime.now(UTC) + timedelta(seconds=15)).isoformat(),
        "scope": {
            "collector_id": "host",
            "project": "app",
            "environment": "prod",
            "service": "api",
            "container_id": "original",
            "anchor": datetime.now(UTC).isoformat(),
            "services": ["api", "db"],
            "containers": {"api": ["original"], "db": ["db-1"], "worker": ["worker-1"]},
            "trace_ids": ["a" * 32],
            "telemetry_profile": "test",
            "limitations": [],
        },
        "query": {
            "capability": capability,
            "service": None,
            "trace_id": None,
            "lookback_minutes": 5,
            **query,
        },
    }


def inspected(cid, **state):
    return {
        "Id": cid,
        "Config": {
            "Tty": False,
            "Env": ["DB_PASSWORD=never-export"],
            "Labels": {
                "com.docker.compose.project": "app",
                "com.docker.compose.service": "api" if cid == "original" else "db",
            },
        },
        "State": {"Status": "exited", "OOMKilled": True, "ExitCode": 137, **state},
        "RestartCount": 2,
    }


def framed(value, channel=1):
    content = value.encode()
    return bytes([channel, 0, 0, 0]) + struct.pack(">I", len(content)) + content


def test_docker_log_frames_json_and_multiline(worker):
    now = datetime.now(UTC).isoformat()
    raw = framed(f'{now} {{"message":"failed","trace_id":"abc","duration_ms":12}}\n')
    raw += framed(f'{now} Traceback:\n{now}   File "app.py", line 2\n', 2)
    records = worker.docker_log_records(raw)
    assert len(records) == 3 and records[0]["trace_id"] == "abc"
    assert records[0]["duration_ms"] == 12
    assert records[-1]["message"].startswith("  File")
    assert (
        worker.docker_log_records(f"{now} tty error\n".encode(), True)[0]["message"] == "tty error"
    )
    with pytest.raises(worker.ReadFailure):
        worker.docker_log_records(raw[:-1])


def test_private_key_bodies_split_across_log_records_are_redacted(worker):
    timestamp = datetime.now(UTC).isoformat()
    raw = framed(
        f"{timestamp} -----BEGIN PRIVATE KEY-----\n{timestamp} never-export-body\n{timestamp} -----END PRIVATE KEY-----\n"
    )
    records = worker.docker_log_records(raw)
    assert "never-export-body" not in json.dumps(records)
    assert "redacted private key" in json.dumps(records)


def test_health_exports_selected_fields_and_pins_original_container(worker):
    paths = []

    def docker(path, **kwargs):
        paths.append(path)
        return inspected(path.split("/")[2])

    result = worker.RuntimeEngine(docker=docker).execute(job())
    assert result["provenance"]["coverage"] == "snapshot"
    assert result["data"]["observations"][0]["oom_killed"] is True
    assert "never-export" not in json.dumps(result)
    assert all("/json" in path for path in paths)
    assert result["provenance"]["container_ids"] == ["original", "db-1"]


@pytest.mark.parametrize("failure", ["gone", "wrong_project", "replacement", "wrong_service"])
def test_missing_or_replaced_container_is_not_substituted(worker, failure):
    def docker(path, **kwargs):
        if failure == "gone":
            raise worker.ReadFailure("http_404")
        data = inspected("original")
        if failure == "replacement":
            data["Id"] = "replacement"
        else:
            data["Config"]["Labels"][
                "com.docker.compose." + ("service" if failure == "wrong_service" else "project")
            ] = "other"
        return data

    result = worker.RuntimeEngine(docker=docker).execute(job(service="api"))
    assert result["data"]["observations"] == []
    assert result["limitations"]


def test_unrelated_trace_logs_are_excluded_and_credentials_redacted(worker):
    task = job("logs", service="api")
    timestamp = (datetime.fromisoformat(task["scope"]["anchor"]) - timedelta(seconds=1)).isoformat()

    def docker(path, **kwargs):
        if path.endswith("/json"):
            return inspected("original")
        return framed(
            f'{timestamp} {{"message":"password=sensitive","trace_id":"{"a" * 32}"}}\n{timestamp} {{"message":"unrelated-sentinel","trace_id":"other"}}\n'
        )

    result = worker.RuntimeEngine(docker=docker).execute(task)
    assert len(result["data"]["observations"]) == 1
    assert "sensitive" not in json.dumps(result) and "unrelated-sentinel" not in json.dumps(result)
    assert any("Excluded 1" in limitation for limitation in result["limitations"])


def test_cpu_memory_counter_reset_and_buffer_eviction(worker):
    raw = {
        "read": datetime.now(UTC).isoformat(),
        "cpu_stats": {
            "cpu_usage": {"total_usage": 200},
            "system_cpu_usage": 1000,
            "online_cpus": 2,
        },
        "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 800},
        "memory_stats": {"usage": 1000, "limit": 2000, "stats": {"inactive_file": 100}},
    }
    point = worker.normalize_stats(raw)
    assert point["cpu_percent"] == 100 and point["memory_bytes"] == 900
    reset = copy.deepcopy(raw)
    reset["cpu_stats"]["cpu_usage"]["total_usage"] = 1
    assert worker.normalize_stats(reset, raw)["counter_reset"] is True
    assert worker.normalize_stats(reset, raw)["cpu_percent"] is None
    buffer = worker.ResourceBuffer(max_containers=1)
    buffer.add("first", raw)
    buffer.add("second", raw)
    assert list(buffer.samples) == ["second"] and buffer.omitted == 1
    old = copy.deepcopy(raw)
    old["read"] = (datetime.now(UTC) - timedelta(minutes=31)).isoformat()
    buffer.add("old", old)
    assert "old" not in buffer.samples


def test_resource_history_is_downsampled_instead_of_dropped(worker):
    engine = worker.RuntimeEngine(docker=lambda path, **kwargs: inspected("original"))
    task = job("resources", service="api")
    end = datetime.fromisoformat(task["scope"]["anchor"])
    for i in range(60):
        engine.buffer.add(
            "original",
            {
                "read": (end - timedelta(seconds=(59 - i) * 5)).isoformat(),
                "memory_stats": {"usage": 1900 if i == 13 else 1000, "limit": 2000},
            },
        )
    result = engine.execute(task)
    assert len(worker.encode(result).encode()) <= 7400
    assert result["data"]["observations"][0]["samples"]
    assert result["data"]["observations"][0]["summary"]["peak_memory"]["memory_bytes"] == 1900
    assert result["data"]["observations"][0]["summary"]["sample_count"] == 60
    assert result["truncated"]


def test_backend_read_budget_shared_across_baseline(worker):
    engine = worker.RuntimeEngine(docker=lambda path, **kwargs: inspected(path.split("/")[2]))
    task = job()
    engine.budgets["incident"] = (30, time.time() + 30)
    result = engine.execute(task)
    assert "backend_read_budget" in json.dumps(result)


def test_worker_honors_server_read_allowance(worker):
    paths = []

    def docker(path, **kwargs):
        paths.append(path)
        return inspected("original")

    result = worker.RuntimeEngine(docker=docker).execute(
        job("logs", service="api") | {"max_reads": 1}
    )
    assert len(paths) == 1
    assert result["status"] == "unavailable" and result["reason"] == "backend_read_budget"


def profile():
    return {
        "profiles": {
            "test": {
                "project": "app",
                "environment": "prod",
                "service_aliases": {"app-api": "api", "jobs": "worker"},
                "scope_labels": {"project": "app", "env": "prod", "collector": "host"},
                "metric_bindings": {"api": {"service": "app-api"}},
                "prometheus": {
                    "url": "http://metrics.test",
                    "templates": {"request_count": "sum(increase(requests_total{{scope}}[5m]))"},
                },
                "tempo": {"url": "http://traces.test"},
            }
        }
    }


def test_prometheus_uses_scoped_templates_and_limits(worker):
    calls = []

    def http(base, path, **kwargs):
        calls.append((base, parse_qs(urlsplit(path).query)))
        return {
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {"secret": "never-export"},
                        "values": [[datetime.now(UTC).timestamp() - 1, "5"]],
                    }
                ]
            },
        }

    result = worker.RuntimeEngine(profile(), http=http).execute(job("latency"))
    params = calls[0][1]
    assert params["limit"] == ["20"] and params["timeout"] == ["5s"] and params["step"] == ["15"]
    assert 'collector="host"' in params["query"][0] and 'project="app"' in params["query"][0]
    assert "never-export" not in json.dumps(result)
    assert result["data"]["metrics"][0]["values"][0][1] == 5


def test_trace_aliases_only_export_permitted_mapped_services(worker):
    span_time = int((datetime.now(UTC) - timedelta(seconds=5)).timestamp() * 1e9)

    def http(base, path, **kwargs):
        assert "start=" in path and "end=" in path and "a" * 32 in path
        return {
            "batches": [
                {
                    "resource": {
                        "attributes": [{"key": "service.name", "value": {"stringValue": name}}]
                    },
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "spanId": "span",
                                    "traceId": "a" * 32,
                                    "name": "operation",
                                    "startTimeUnixNano": span_time,
                                    "endTimeUnixNano": span_time + 1000000,
                                    "attributes": [
                                        {
                                            "key": "password",
                                            "value": {"stringValue": "never-export"},
                                        }
                                    ],
                                    "status": {"code": 2},
                                }
                            ]
                        }
                    ],
                }
                for name in ["jobs", "unknown"]
            ]
        }

    result = worker.RuntimeEngine(profile(), http=http).execute(job("traces"))
    assert len(result["data"]["spans"]) == 1
    assert result["data"]["spans"][0]["service"] == "worker" and result["data"]["spans"][0]["error"]
    assert "never-export" not in json.dumps(result)


@pytest.mark.parametrize(
    "capability,reason",
    [("traces", "tempo_not_configured"), ("latency", "prometheus_not_configured")],
)
def test_optional_telemetry_reports_unavailable(worker, capability, reason):
    result = worker.RuntimeEngine().execute(job(capability))
    assert result["status"] == "unavailable" and result["reason"] == reason


def test_transport_rejects_writes_redirect_destinations_and_response_limits(worker):
    transport = importlib.import_module("runtime_transport")
    for path in [
        "/containers/a/exec",
        "/containers/a/start",
        "/images/json",
        "/containers/a/json\r\nInjected: yes",
    ]:
        with pytest.raises(transport.ReadFailure):
            transport.docker_get(path)
    for base in ["file:///etc/passwd", "http://user:pass@host", "http://host/?query=1"]:
        with pytest.raises(transport.ReadFailure):
            transport.http_request(base, "/api")

    class Response:
        status = 200

        def getheader(self, *args):
            return "identity"

        def read1(self, size):
            return b"x" * size

    class Connection:
        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    with pytest.raises(transport.ReadFailure, match="upstream_response_limit"):
        transport.read_response(Connection(), "GET", "/")
