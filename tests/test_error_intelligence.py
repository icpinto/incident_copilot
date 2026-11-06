import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from incident_copilot.dashboard.api import router
from incident_copilot.dashboard.issue_capture import normalize_message
from incident_copilot.dashboard.issue_models import IssueFilters, IssueUpdate
from incident_copilot.dashboard.models import IncidentSubmission, SettingsUpdate
from incident_copilot.dashboard.service import DashboardService
from incident_copilot.dashboard.store import ControlStore


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("IC_CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("IC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("IC_INDEX_DB_PATH", str(tmp_path / "index.db"))
    result = DashboardService(ControlStore(tmp_path / "control.db"))
    for key in ("repo", "other"):
        result.store.save_repository(
            key,
            {"name": key, "source_type": "local", "repo_root": str(tmp_path), "compose_path": None},
        )
    return result


@pytest.fixture
def client(service):
    app = FastAPI()
    app.state.dashboard = service
    app.include_router(router)
    with TestClient(app) as value:
        yield value


def upload(service, text="api | ValueError: failed 123", repo="repo", original=None):
    return service.submit(
        IncidentSubmission(repository_id=repo, title="Example", log_text=text), original=original
    )


def all_issues(service):
    return service.issues.list(IssueFilters())["items"]


def test_capture_all_groups_chains_and_context(service):
    text = "\n".join(
        [
            json.dumps({"service": "api", "level": "info", "message": "Started"}),
            json.dumps(
                {
                    "service": "api",
                    "trace_id": "a",
                    "message": 'Traceback (most recent call last):\n  File "/app/api.py", line 2, in handle\nValueError: failed 1\n\nThe above exception was the direct cause of the following exception:\nTraceback (most recent call last):\n  File "/app/api.py", line 8, in handle\nRuntimeError: failed 2',
                }
            ),
            json.dumps({"service": "worker", "trace_id": "b", "message": "TypeError: wrong"}),
        ]
    )
    run = upload(service, text)
    assert run.error_tracking_status == "tracked" and run.status == "queued"
    data = service.issues.intelligence(IssueFilters(), "service")
    assert data["summary"]["occurrences"] == 2
    assert data["summary"]["fallback_timestamp_count"] == 2
    assert {item["value"] for item in data["hotspots"]} == {"api", "worker"}
    assert sum(point["occurrences"] for point in data["trends"]) == 2
    with service.store.connection() as db:
        events = [json.loads(row[0]) for row in db.execute("SELECT payload FROM error_occurrences")]
    assert sorted(len(event["exceptions"]) for event in events) == [1, 2]
    assert all(event["group_id"] for event in events)


def test_duplicates_reruns_concurrency_and_restart(service):
    with ThreadPoolExecutor(max_workers=4) as pool:
        runs = list(pool.map(lambda _: upload(service), range(8)))
    assert len({run.error_batch_id for run in runs}) == 1
    assert sum(run.error_tracking_status == "tracked" for run in runs) == 1
    source = service.store.get("incidents", runs[0].id)
    rerun = upload(service, original=source)
    assert rerun.error_batch_id == runs[0].error_batch_id
    restarted = DashboardService(ControlStore(service.store.path))
    assert restarted.issues.intelligence(IssueFilters(), "service")["summary"]["occurrences"] == 1
    issue = all_issues(restarted)[0]
    occurrences = restarted.issues.occurrences(issue["id"], IssueFilters(), 1, 20)
    assert len(occurrences["items"][0]["investigations"]) == 9
    upload(service, repo="other")
    assert len(all_issues(service)) == 2


def test_fingerprint_masks_dynamic_values_but_preserves_service_and_stack(service):
    for number, line in ((123, 1), (456, 99)):
        upload(
            service,
            f'api | ValueError: failed {number}\n  File "/app/api.py", line {line}, in handle',
        )
    issues = all_issues(service)
    assert len(issues) == 1 and issues[0]["total_count"] == 2
    upload(service, 'worker | ValueError: failed 123\n  File "/app/api.py", line 1, in handle')
    assert len(all_issues(service)) == 2
    assert normalize_message("request_id=abc 2026-01-01T10:20:30Z user 123") == normalize_message(
        "request_id=xyz 2026-02-02T11:22:33Z user 456"
    )


def test_lifecycle_recurrence_and_late_uploads(service):
    upload(service)
    issue = all_issues(service)[0]
    issue_id = issue["id"]
    service.issues.update(
        issue_id, IssueUpdate(status="resolved", owner="Payments", resolution_note="Fixed")
    )
    # Exact replay and timestamped older data cannot reopen the issue.
    upload(service)
    upload(service, "api | 2020-01-01T00:00:00Z ValueError: failed 456")
    assert service.issues.detail(issue_id)["status"] == "resolved"
    future = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    upload(service, f"api | {future} ValueError: failed 789")
    assert service.issues.detail(issue_id)["status"] == "open"
    assert any(
        item["kind"] == "reopened" for item in service.issues.history(issue_id, 1, 20)["items"]
    )
    service.issues.update(issue_id, IssueUpdate(status="ignored"))
    upload(service, "api | ValueError: failed 1000")
    assert service.issues.detail(issue_id)["status"] == "ignored"


def test_merge_split_routing_conservation_and_rollback(service):
    for text in (
        "api | ValueError: first 1",
        "api | ValueError: first 2",
        "api | TypeError: second",
    ):
        upload(service, text)
    issues = all_issues(service)
    target, source = issues[0], issues[1]
    foreign = upload(service, "api | RuntimeError: foreign", "other")
    foreign_issue = service.issues.list(IssueFilters(incident_id=foreign.id))["items"][0]
    with pytest.raises(ValueError, match="same repository"):
        service.issues.merge(target["id"], [source["id"], foreign_issue["id"]])
    assert service.issues.detail(source["id"])["redirected_from"] is None
    service.issues.update(target["id"], IssueUpdate(owner="Team", status="resolved"))
    merged = service.issues.merge(target["id"], [source["id"]])
    assert (
        merged["total_count"] == 3 and merged["status"] == "resolved" and merged["owner"] == "Team"
    )
    assert service.issues.detail(source["id"])["id"] == target["id"]
    records = service.issues.occurrences(target["id"], IssueFilters(), 1, 20)["items"]
    with pytest.raises(ValueError, match="proper subset"):
        service.issues.split(target["id"], [row["id"] for row in records])
    split = service.issues.split(target["id"], [records[0]["id"]], "Separate")
    assert split["total_count"] == 1 and split["owner"] == "Team" and split["status"] == "open"
    upload(service, "api | TypeError: second 999")
    # Different normalized message is a new signature; replaying a previously merged signature follows target.
    upload(service, "api | ValueError: first 3")
    assert service.issues.detail(split["id"])["total_count"] == 1
    assert service.issues.detail(target["id"])["total_count"] == 3


def test_time_windows_filters_hotspots_and_zero_baseline(service):
    upload(
        service,
        'api | 2026-10-10T01:00:00+01:00 ValueError: failed 1\n  File "/app/a.py", line 1, in handle',
    )
    upload(
        service,
        'api | 2026-10-24T00:00:00Z ValueError: failed 2\n  File "/app/a.py", line 8, in handle',
    )
    upload(service, "worker | 2026-10-25T00:00:00Z TypeError: new")
    filters = IssueFilters(
        start_at=datetime(2026, 10, 20, tzinfo=UTC), end_at=datetime(2026, 11, 3, tzinfo=UTC)
    )
    data = service.issues.intelligence(filters, "service")
    assert data["summary"]["occurrences"] == 2 and data["summary"]["previous_occurrences"] == 1
    assert data["summary"]["change_percent"] == 100
    assert data["summary"]["new_issues"] == 1
    data = service.issues.intelligence(
        filters.model_copy(update={"file_path": "/app/a.py"}), "function"
    )
    assert data["hotspots"] == [{"value": "handle", "label": "handle", "occurrences": 1}]
    assert data["summary"]["source_timestamp_count"] == 1
    for key, value in (("service_name", "worker"), ("error_class", "TypeError")):
        rows = service.issues.list(filters.model_copy(update={key: value}))["items"]
        assert len(rows) == 1 and rows[0]["change_percent"] is None


def test_legacy_and_schema_are_idempotent(service):
    tracking = service.store.config("error_tracking")
    run = upload(service)
    source = service.store.get("incidents", run.id)
    # A genuinely historical record has no batch metadata or batch link.
    source["id"] = "legacy"
    source.pop("error_batch_id")
    source.pop("error_tracking_status")
    service.store.put("incidents", "legacy", source)
    rerun = upload(service, original=source)
    assert rerun.error_tracking_status == "untracked" and rerun.error_batch_id is None
    assert ControlStore(service.store.path).config("error_tracking") == tracking
    assert service.issues.intelligence(IssueFilters(), "service")["summary"]["occurrences"] == 1


def test_api_validation_links_and_redaction(client, service):
    service.configuration.save(
        SettingsUpdate(values={"openai_compatible_api_key": "private-value"})
    )
    run = client.post(
        "/api/incidents",
        json={"repository_id": "repo", "log_text": "api | ValueError: private-value"},
    ).json()
    data = client.get(f"/api/issues?incident_id={run['id']}").json()
    issue_id = data["items"][0]["id"]
    assert "private-value" not in json.dumps(data)
    for path in (
        f"/api/issues/{issue_id}",
        f"/api/issues/{issue_id}/occurrences",
        f"/api/issues/{issue_id}/activity",
        "/api/error-intelligence",
    ):
        response = client.get(path)
        assert response.status_code == 200 and "private-value" not in response.text
    for query in (
        "page=0",
        "status=broken",
        "sort=bad",
        "start_at=2026-10-01T00:00:00",
        "start_at=2030-01-01T00:00:00Z",
    ):
        assert client.get(f"/api/issues?{query}").status_code == 422
    assert client.get("/api/error-intelligence?dimension=invalid").status_code == 422
    assert client.get("/api/issues/missing").status_code == 404
    assert client.patch(f"/api/issues/{issue_id}", json={"title": " "}).status_code == 422
    assert (
        client.patch(
            f"/api/issues/{issue_id}", json={"owner": "Payments", "status": "investigating"}
        ).status_code
        == 200
    )
    assert client.get("/api/issues?owner=Payments&status=investigating").json()["total"] == 1
    assert (
        client.post(f"/api/issues/{issue_id}/split", json={"occurrence_ids": []}).status_code == 422
    )


def test_failed_analysis_does_not_remove_observations(service):
    run = upload(service)
    service.store.update(
        "incidents", run.id, {"status": "failed", "error": "Repository inaccessible"}
    )
    assert service.issues.intelligence(IssueFilters(), "service")["summary"]["occurrences"] == 1


def test_api_grouping_actions_pagination_and_overlapping_frames(client, service):
    for number in (1, 2):
        upload(service, f'api | ValueError: bad {number}\n  File "/app/a.py", line 1, in handle\n  File "/app/a.py", line 2, in handle')
    upload(service, "worker | TypeError: other")
    first = client.get("/api/issues?page_size=1").json()
    second = client.get("/api/issues?page_size=1&page=2").json()
    assert first["total"] == 2 and first["items"][0]["id"] != second["items"][0]["id"]
    target, source = first["items"][0]["id"], second["items"][0]["id"]
    hotspots = client.get("/api/error-intelligence?dimension=file&service_name=api").json()
    assert hotspots["hotspots"] == [{"value": "/app/a.py", "label": "/app/a.py", "occurrences": 2}]
    merged = client.post(f"/api/issues/{target}/merge", json={"source_issue_ids": [source]})
    assert merged.status_code == 200 and merged.json()["total_count"] == 3
    assert client.get(f"/api/issues/{source}").json()["redirected_from"] == source
    record = client.get(f"/api/issues/{target}/occurrences?page_size=1").json()
    assert record["total"] == 3 and len(record["items"]) == 1
    split = client.post(f"/api/issues/{target}/split", json={"occurrence_ids": [record["items"][0]["id"]]})
    assert split.status_code == 201 and split.json()["total_count"] == 1
    assert client.get(f"/api/issues/{target}/activity?page_size=1").json()["total"] >= 4
    assert client.get("/api/overview").json()["total"] == 3


def test_structural_metadata_and_observed_service_are_not_inferred(client, service):
    service.configuration.save(SettingsUpdate(values={"openai_compatible_api_key": "service"}))
    result = client.post("/api/incidents", json={"repository_id": "repo", "log_text": "ValueError: failed", "user_hint_service": "imagined"})
    assert result.status_code == 202
    data = client.get("/api/error-intelligence").json()
    assert data["dimension"] == "service" and data["hotspots"][0]["value"] == "(unknown)"


def test_submit_rollback_leaves_no_partial_incident(service, monkeypatch):
    import incident_copilot.dashboard.issue_capture as capture

    original = capture.activity

    def broken(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("interrupted")

    monkeypatch.setattr(capture, "activity", broken)
    with pytest.raises(RuntimeError, match="interrupted"):
        upload(service)
    with service.store.connection() as db:
        for table in (
            "incidents",
            "error_batches",
            "error_issues",
            "error_occurrences",
            "error_fingerprints",
        ):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
