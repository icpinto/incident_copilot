"""Scope, durability and diagnosis checks without a Docker daemon or external model."""

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from incident_copilot.dashboard import runtime_evidence as runtime
from incident_copilot.dashboard.deployments import context, observe
from incident_copilot.dashboard.issue_capture import prepare_upload
from incident_copilot.dashboard.models import IncidentSubmission
from incident_copilot.dashboard.service import DashboardService
from incident_copilot.dashboard.store import ControlStore
from incident_copilot.schemas.deployment import ReleaseMetadata
from incident_copilot.schemas.investigation import EvidenceItem, EvidenceRequest
from incident_copilot.schemas.runtime import RuntimeQuery
from tests.action_plan_fixtures import action_plan


@pytest.fixture
def setup_runtime(tmp_path, monkeypatch):
    for name, value in {
        "IC_CONTROL_DB_PATH": str(tmp_path / "control.db"),
        "IC_DATA_DIR": str(tmp_path / "data"),
        "IC_INDEX_DB_PATH": str(tmp_path / "index.db"),
        "IC_GUARDRAILS_MODE": "off",
        "IC_CODE_GRAPH_ENABLED": "false",
    }.items():
        monkeypatch.setenv(name, value)
    store = ControlStore(tmp_path / "control.db")
    service = DashboardService(store)
    store.put(
        "repositories",
        "repo",
        {
            "id": "repo",
            "name": "App",
            "source_type": "local",
            "repo_root": str(tmp_path),
            "compose_path": None,
            "archived": False,
        },
    )
    now = datetime.now(UTC).isoformat()
    with store.connection() as db:
        db.execute(
            "INSERT INTO collector_sources(collector_id,project,services,repository_id,enabled,last_seen,environment,runtime_dependencies) VALUES ('host','app',?,'repo',1,?,'prod',?)",
            (json.dumps(["api", "db", "worker"]), now, json.dumps({"api": ["db"]})),
        )
        for name, cid in [("api", "api-old"), ("db", "db-1"), ("worker", "worker-1")]:
            observed = observe(
                db,
                collector_id="host",
                project="app",
                service=name,
                container_id=cid,
                release=ReleaseMetadata(),
                now=now,
            )
            if name == "api":
                deployment = context(observed, {"environment": "prod"})
        runtime.register(db, "host", ["logs", "resources", "traces", "dependencies", "latency"])
    text = json.dumps(
        {
            "service": "api",
            "timestamp": now,
            "trace_id": "a" * 32,
            "message": "ConnectionError: database unavailable",
        }
    )
    capture = prepare_upload(text, lambda value: value)
    run = service.submit(
        IncidentSubmission(repository_id="repo", log_text=text),
        deployment_context=deployment,
        prepared_capture=capture,
    )
    return service, store.get("incidents", run.id)


def result_for(job, **updates):
    query, scope = job["query"], job["scope"]
    result = {
        "capability": query["capability"],
        "status": "ok",
        "reason": None,
        "provenance": {
            "collector_id": scope["collector_id"],
            "project": scope["project"],
            "environment": scope["environment"],
            "service": query.get("service") or scope["service"],
            "container_ids": [scope["container_id"]],
            "requested_start": (
                datetime.fromisoformat(scope["anchor"])
                - timedelta(minutes=query["lookback_minutes"])
            ).isoformat(),
            "requested_end": scope["anchor"],
            "observed_at": datetime.now(UTC).isoformat(),
            "coverage": "snapshot",
        },
        "data": {
            "observations": [{"service": "api", "container_id": "api-old", "oom_killed": True}]
        },
        "limitations": ["Snapshot at collection time."],
        "truncated": False,
        "duration_ms": 2,
    }
    return result | updates


def finish_baseline(service, record):
    while True:
        jobs = runtime.claim(service.store, "host", [], {})["jobs"]
        if not jobs:
            break
        for job in jobs:
            runtime.accept(
                service.store,
                "host",
                job["id"],
                job["lease"],
                result_for(job),
                service.configuration.redact,
            )


def test_baseline_is_atomic_and_does_not_overwrite_activity_evidence(setup_runtime):
    service, record = setup_runtime
    assert record["runtime_scope"]["services"] == ["api", "db"]
    assert record["runtime_scope"]["containers"]["api"] == ["api-old"]
    assert len(runtime.snapshot(service.store, record["id"])["runtime_collection"]) == 5
    service.store.update(
        "incidents",
        record["id"],
        {"evidence": {"raw_log": "kept", "deep_analysis": {"file.py": {"ok": True}}}},
    )
    finish_baseline(service, record)
    public = service.public(service.store.get("incidents", record["id"]))
    assert public.evidence.deep_analysis == {"file.py": {"ok": True}}
    assert len(public.evidence.runtime_evidence) == 5
    assert all(
        item.repository is None and item.revision is None
        for item in public.evidence.runtime_evidence
    )


def test_claim_concurrency_duplicate_and_restart(setup_runtime):
    service, _record = setup_runtime
    jobs = runtime.claim(service.store, "host", [], {})["jobs"]
    assert len(jobs) == 2
    assert jobs[0]["query"]["capability"] == "traces"
    assert runtime.claim(service.store, "host", [], {})["jobs"] == []
    job = jobs[0]
    assert runtime.accept(
        service.store, "host", job["id"], job["lease"], result_for(job), lambda x: x
    )["accepted"]
    restarted = ControlStore(service.store.path)
    assert (
        runtime.accept(restarted, "host", job["id"], job["lease"], result_for(job), lambda x: x)[
            "reason"
        ]
        == "duplicate"
    )
    assert len(runtime.claim(restarted, "host", [], {})["jobs"]) == 1


@pytest.mark.parametrize(
    "change",
    [
        "host",
        "project",
        "environment",
        "service",
        "container",
        "time",
        "capability",
        "large",
        "lease",
    ],
)
def test_results_reject_wrong_scope_and_limits(setup_runtime, change):
    service, _ = setup_runtime
    job = runtime.claim(service.store, "host", [], {})["jobs"][0]
    result = result_for(job)
    if change in {"project", "environment", "service"}:
        result["provenance"][change] = "other"
    elif change == "host":
        result["provenance"]["collector_id"] = "other"
    elif change == "container":
        result["provenance"]["container_ids"] = ["api-replacement"]
    elif change == "time":
        result["provenance"]["requested_end"] = datetime.now(UTC).isoformat()
    elif change == "capability":
        result["capability"] = "logs"
    elif change == "large":
        result["data"]["large"] = "a" * 9000
    with pytest.raises(ValueError):
        runtime.accept(
            service.store,
            "host",
            job["id"],
            "bad" if change == "lease" else job["lease"],
            result,
            lambda x: x,
        )


def test_lease_expiry_retries_without_resetting_deadline(setup_runtime):
    service, _ = setup_runtime
    job = runtime.claim(service.store, "host", [], {})["jobs"][0]
    with service.store.connection() as db:
        db.execute(
            "UPDATE runtime_requests SET lease_until=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), job["id"]),
        )
    retry = runtime.claim(service.store, "host", [], {})["jobs"][0]
    assert (
        retry["id"] == job["id"]
        and retry["lease"] != job["lease"]
        and retry["deadline"] == job["deadline"]
    )
    with pytest.raises(ValueError):
        runtime.accept(service.store, "host", job["id"], job["lease"], result_for(job), lambda x: x)


def test_backend_read_allowances_survive_lost_claims_and_restarts(setup_runtime):
    service, record = setup_runtime
    allowances = 0
    for _ in range(20):
        restarted = ControlStore(service.store.path)
        jobs = runtime.claim(restarted, "host", [], {})["jobs"]
        allowances += sum(job["max_reads"] for job in jobs)
        with restarted.connection() as db:
            db.execute(
                "UPDATE runtime_requests SET lease_until=? WHERE state='leased'",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
            )
        if not jobs:
            break
    assert allowances <= 30
    with service.store.connection() as db:
        assert (
            db.execute(
                "SELECT used FROM runtime_read_reservations WHERE incident_id=?", (record["id"],)
            ).fetchone()[0]
            == allowances
        )
    assert any(
        item["status"] == "budget_exhausted"
        for item in runtime.snapshot(service.store, record["id"])["runtime_collection"]
    )


def test_disabled_source_and_terminal_incident_stop_queries(setup_runtime):
    service, record = setup_runtime
    job = runtime.claim(service.store, "host", [], {})["jobs"][0]
    with service.store.connection() as db:
        db.execute("UPDATE collector_sources SET runtime_enabled=0")
    with pytest.raises(ValueError):
        runtime.accept(service.store, "host", job["id"], job["lease"], result_for(job), lambda x: x)
    assert runtime.claim(service.store, "host", [], {})["jobs"] == []
    service.store.update("incidents", record["id"], {"status": "canceled"})
    assert not any(
        item["status"] in {"queued", "leased"}
        for item in runtime.snapshot(service.store, record["id"])["runtime_collection"]
    )


def test_only_observed_trace_can_expand_services(setup_runtime):
    service, record = setup_runtime
    job = runtime.claim(service.store, "host", [], {})["jobs"][0]
    result = result_for(job, data={"spans": [{"service": "worker", "trace_id": "a" * 32}]})
    with service.store.connection() as db:
        assert "worker" not in runtime.effective_scope(db, record).services
    bad = copy.deepcopy(result)
    bad["data"]["spans"][0]["trace_id"] = "unrelated"
    with pytest.raises(ValueError):
        runtime.accept(service.store, "host", job["id"], job["lease"], bad, lambda x: x)
    runtime.accept(service.store, "host", job["id"], job["lease"], result, lambda x: x)
    with service.store.connection() as db:
        scope = runtime.effective_scope(db, record)
    assert "worker" in scope.services
    with pytest.raises(ValueError):
        runtime.validate_query(RuntimeQuery(capability="logs", service="outside"), scope)
    with pytest.raises(ValueError):
        runtime.validate_query(RuntimeQuery(capability="traces", trace_id="unrelated"), scope)


def test_redaction_and_immutable_rerun(setup_runtime):
    service, record = setup_runtime
    job = runtime.claim(service.store, "host", [], {})["jobs"][0]
    runtime.accept(
        service.store,
        "host",
        job["id"],
        job["lease"],
        result_for(job, data={"message": "password=private-value"}),
        lambda x: x,
    )
    item = runtime.snapshot(service.store, record["id"])["runtime_evidence"][0]
    assert "private-value" not in json.dumps(item)
    rerun = service.submit(
        IncidentSubmission(repository_id="repo", log_text="ValueError: rerun"), original=record
    )
    saved = service.store.get("incidents", rerun.id)
    assert saved["runtime_scope"] == record["runtime_scope"]
    assert len(runtime.snapshot(service.store, rerun.id)["runtime_evidence"]) == 1
    assert all(
        item["status"] not in {"queued", "leased"}
        for item in runtime.snapshot(service.store, rerun.id)["runtime_collection"]
    )


@pytest.mark.parametrize(
    "args",
    [
        {"capability": "logs", "service": "api", "query": "arbitrary"},
        {"capability": "logs", "lookback_minutes": 16},
        {"capability": "logs", "file_path": "/etc/passwd"},
    ],
)
def test_runtime_tool_rejects_arbitrary_arguments(args):
    with pytest.raises(ValidationError):
        EvidenceRequest(tool="get_runtime_evidence", question="Why?", **args)


def test_source_identity_cannot_be_attached_to_runtime_evidence(setup_runtime):
    service, _ = setup_runtime
    job = runtime.claim(service.store, "host", [], {})["jobs"][0]
    with pytest.raises(ValidationError):
        EvidenceItem(
            id="x",
            kind="runtime_logs",
            repository="repo",
            revision="unverified",
            runtime=result_for(job)["provenance"],
        )


@pytest.mark.asyncio
async def test_runtime_only_single_pass_diagnosis_and_citations(setup_runtime, monkeypatch):
    from incident_copilot.graph.diagnosis_graph import run_diagnosis_graph
    from incident_copilot.graph.nodes import build_hypotheses
    from incident_copilot.reporting.markdown_renderer import render_markdown
    from incident_copilot.reporting.report_builder import build_report

    service, record = setup_runtime
    finish_baseline(service, record)
    captured = runtime.snapshot(service.store, record["id"])
    ref = captured["runtime_evidence"][0]["id"]

    class Model:
        async def complete(self, **kwargs):
            assert ref in kwargs["prompt"] and "oom_killed" in kwargs["prompt"]
            return json.dumps(
                {
                    "diagnosis_status": "provisional",
                    "diagnosis_reason": "Observed OOM state supports resource exhaustion.",
                    "next_checks": [],
                    "hypotheses": [
                        {
                            "rank": 1,
                            "title": "Resource exhaustion",
                            "failure_layer": "infrastructure",
                            "explanation": "An OOM termination interrupted the dependency.",
                            "evidence": ["OOM state"],
                            "evidence_refs": [ref], "action_plan": action_plan([ref]),
                            "confidence": 0.8,
                            "next_checks": [],
                        }
                    ],
                }
            )

    monkeypatch.setattr(build_hypotheses, "get_client", lambda *a, **k: Model())
    state = await run_diagnosis_graph(
        {"incident_id": record["id"], "candidates": [], "source_unavailable": True, **captured},
        service.configuration.effective().model_copy(update={"agentic_enabled": False}),
    )
    report = build_report(state)
    assert report.hypotheses[0].failure_layer == "infrastructure"
    assert report.hypotheses[0].evidence_refs == [ref]
    assert "Runtime Collection" in render_markdown(report)
    assert report.hypotheses[0].confidence is None


@pytest.mark.asyncio
async def test_missing_deployed_source_continues_stages(setup_runtime):
    from incident_copilot.dashboard.activities import dashboard_stage_activity

    service, record = setup_runtime
    inputs = record["input"] | {
        "version_linked": True,
        "deployment_context": record["deployment_context"],
        "repository_profile": {"source_type": "local"},
        "source_identity": "x",
        "repository_cache_dir": str(service.store.path.parent / "cache"),
    }
    service.store.update(
        "incidents", record["id"], {"input": inputs, "source_resolution": {"status": "pending"}}
    )
    await dashboard_stage_activity(record["id"], "indexing")
    saved = service.store.get("incidents", record["id"])
    assert saved["input"]["source_unavailable"] is True
    assert saved["input"]["repo_root"] is None
    assert saved["source_resolution"]["status"] == "unavailable"
    await dashboard_stage_activity(record["id"], "correlating")
    await dashboard_stage_activity(record["id"], "analyzing")
    assert service.store.get("incidents", record["id"])["evidence"]["candidates"] == []


@pytest.mark.asyncio
async def test_missing_unversioned_repository_continues_runtime_diagnosis(setup_runtime):
    from incident_copilot.dashboard.activities import dashboard_stage_activity

    service, record = setup_runtime
    service.store.update(
        "incidents",
        record["id"],
        {
            "input": record["input"]
            | {"repo_root": str(service.store.path.parent / "missing-repository")}
        },
    )
    await dashboard_stage_activity(record["id"], "indexing")
    saved = service.store.get("incidents", record["id"])
    assert saved["input"]["source_unavailable"] is True
    assert saved["source_resolution"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_runtime_snapshot_is_excluded_from_other_primary_group(setup_runtime, monkeypatch):
    from incident_copilot.correlation.log_parser import parse_logs
    from incident_copilot.dashboard import activities

    service, record = setup_runtime
    finish_baseline(service, record)
    now = datetime.now(UTC).isoformat()
    first = json.dumps(
        {"service": "api", "timestamp": now, "trace_id": "a" * 32, "message": "ValueError: first"}
    )
    second = json.dumps(
        {"service": "api", "timestamp": now, "trace_id": "b" * 32, "message": "ValueError: second"}
    )
    parsed = parse_logs(first + "\n" + second)
    parsed.incident_context.primary_group_id = parsed.incident_context.groups[1].id
    scope = record["runtime_scope"] | {"trigger_event_id": parsed.incident_context.events[0].id}
    service.store.update(
        "incidents",
        record["id"],
        {
            "runtime_scope": scope,
            "evidence": {"raw_log": first + "\n" + second, "parsed_log": parsed.model_dump()},
        },
    )

    async def graph(payload, settings, runtime_context=None):
        assert payload["runtime_evidence"] == []
        assert payload["runtime_scope"] is None and runtime_context.runtime_client is None
        assert payload["runtime_collection"][0]["status"] == "unavailable"
        return payload | {
            "hypotheses": [],
            "diagnosis_status": "insufficient_evidence",
            "diagnosis_reason": "No associated runtime observations.",
        }

    monkeypatch.setattr(activities, "run_diagnosis_graph", graph)
    await activities.dashboard_stage_activity(record["id"], "reasoning")
    report = service.store.get("incidents", record["id"])["report"]
    assert report["retrieved_evidence"] == []
    assert "another log group" in report["runtime_collection"][0]["reason"]


def test_runtime_endpoints_authentication_limits_and_disable(setup_runtime, monkeypatch):
    from fastapi.testclient import TestClient

    import incident_copilot.dashboard.api as dashboard_api
    from apps.api.main import app

    service, _record = setup_runtime
    monkeypatch.setenv("IC_LOG_INGEST_TOKEN", "runtime-test-token")

    async def idle(_):
        await asyncio.sleep(3600)

    monkeypatch.setattr(dashboard_api, "reconciliation_loop", idle)
    headers = {"Authorization": "Bearer runtime-test-token"}
    with TestClient(app) as client:
        url = "/api/collector/runtime/claim"
        assert client.post(url, json={}).status_code == 401
        assert client.post(url, json={}, headers=headers).status_code == 422
        assert client.post(url, content="a" * 9000, headers=headers).status_code == 413
        response = client.post(
            url, json={"collector_id": "host", "capabilities": ["logs"]}, headers=headers
        )
        assert response.status_code == 200
        job = response.json()["jobs"][0]
        body = {
            "collector_id": "host",
            "request_id": job["id"],
            "lease": job["lease"],
            "result": result_for(job),
        }
        assert client.post("/api/collector/runtime/results", json=body).status_code == 401
        assert client.post("/api/collector/runtime/results", json=body, headers=headers).json() == {
            "accepted": True
        }
        assert (
            client.post("/api/collector/runtime/results", json=body, headers=headers).json()[
                "reason"
            ]
            == "duplicate"
        )
        assert (
            client.put(
                "/api/log-sources/app?collector_id=host",
                json={"repository_id": "repo", "enabled": True, "runtime_enabled": False},
            ).status_code
            == 200
        )
        assert (
            client.put(
                "/api/log-sources/app?collector_id=host",
                json={"repository_id": "repo", "enabled": True},
            ).json()["runtime_enabled"]
            is False
        )
        assert (
            client.post(
                url, json={"collector_id": "host", "capabilities": []}, headers=headers
            ).json()["jobs"]
            == []
        )
    with service.store.connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM runtime_requests WHERE state IN ('queued','leased')"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_runtime_only_investigator_can_request_followup_without_source(
    setup_runtime, monkeypatch
):
    from incident_copilot.graph import investigation
    from incident_copilot.graph.diagnosis_graph import run_diagnosis_graph
    from incident_copilot.graph.investigation_runtime import InvestigationRuntime

    service, record = setup_runtime
    finish_baseline(service, record)
    captured = runtime.snapshot(service.store, record["id"])
    model_calls = []

    class Model:
        async def complete(self, **kwargs):
            prompt = json.loads(kwargs["prompt"])
            model_calls.append(prompt)
            if len(model_calls) == 1:
                return json.dumps(
                    {
                        "action": "retrieve",
                        "diagnosis_status": "insufficient_evidence",
                        "diagnosis_reason": "Need shorter resource window.",
                        "hypotheses": [],
                        "next_checks": [],
                        "unresolved_gaps": [],
                        "requests": [
                            {
                                "tool": "get_runtime_evidence",
                                "question": "Was resource exhaustion observed?",
                                "capability": "resources",
                                "service": "api",
                                "lookback_minutes": 1,
                            }
                        ],
                    }
                )
            ref = next(
                item["id"]
                for item in prompt["evidence"]
                if item.get("runtime")
                and item["runtime"]["requested_start"]
                != captured["runtime_evidence"][0]["runtime"]["requested_start"]
            )
            return json.dumps(
                {
                    "action": "finish",
                    "requests": [],
                    "diagnosis_status": "provisional",
                    "diagnosis_reason": "Runtime evidence supports resource exhaustion.",
                    "next_checks": [],
                    "unresolved_gaps": [],
                    "hypotheses": [
                        {
                            "id": "h1",
                            "rank": 1,
                            "title": "Resource exhaustion",
                            "failure_layer": "infrastructure",
                            "explanation": "Observed OOM termination interrupted requests.",
                            "evidence": ["OOM state"],
                            "confidence": 0.8,
                            "next_checks": [],
                            "evidence_refs": [ref], "action_plan": action_plan([ref]),
                            "contradicting_evidence_refs": [],
                        }
                    ],
                }
            )

    monkeypatch.setattr(investigation, "get_client", lambda *a, **kw: Model())
    client = runtime.RuntimeEvidenceClient(service.store, record["id"])
    config = service.configuration.effective().model_copy(update={"agentic_enabled": True})
    payload = {
        "incident_id": record["id"],
        "candidates": [],
        "source_unavailable": True,
        "runtime_scope": record["runtime_scope"],
        **captured,
    }
    context = InvestigationRuntime(
        service.store.path.parent,
        service.store.path.parent / "no-index.db",
        "runtime-only",
        runtime_client=client,
    )
    task = asyncio.create_task(run_diagnosis_graph(payload, config, runtime_context=context))

    async def deliver():
        while not task.done():
            for job in runtime.claim(service.store, "host", [], {})["jobs"]:
                runtime.accept(
                    service.store, "host", job["id"], job["lease"], result_for(job), lambda x: x
                )
            await asyncio.sleep(0.05)

    pump = asyncio.create_task(deliver())
    try:
        state = await asyncio.wait_for(task, 8)
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
    assert len(model_calls) == 2
    assert model_calls[0]["source_unavailable"] is True
    assert state["hypotheses"][0]["failure_layer"] == "infrastructure"
    assert state["investigation"]["tool_calls"] == 1
    assert not (service.store.path.parent / "no-index.db").exists()


@pytest.mark.asyncio
async def test_runtime_followup_budget_is_durable(setup_runtime):
    service, record = setup_runtime
    with service.store.connection() as db:
        for minutes in range(1, 5):
            key = runtime.enqueue(
                db,
                record,
                RuntimeQuery(capability="resources", lookback_minutes=minutes).model_dump(
                    mode="json"
                ),
            )
            db.execute("UPDATE runtime_requests SET state='expired' WHERE id=?", (key,))
    client = runtime.RuntimeEvidenceClient(ControlStore(service.store.path), record["id"])
    outcome = await client.execute(
        EvidenceRequest(
            tool="get_runtime_evidence",
            question="More?",
            capability="resources",
            lookback_minutes=6,
        ),
        "req",
    )
    assert outcome.reason == "runtime_request_limit"


@pytest.mark.asyncio
async def test_runtime_followup_cancellation_stops_work(setup_runtime):
    service, record = setup_runtime
    client = runtime.RuntimeEvidenceClient(service.store, record["id"])
    task = asyncio.create_task(
        client.execute(
            EvidenceRequest(
                tool="get_runtime_evidence",
                question="More?",
                capability="resources",
                lookback_minutes=1,
            ),
            "req",
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with service.store.connection() as db:
        row = db.execute(
            "SELECT state FROM runtime_requests WHERE json_extract(payload,'$.baseline')=0"
        ).fetchone()
    assert row["state"] == "canceled"
