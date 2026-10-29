"""Real local Git fixtures exercise manual sync without network or GitHub credentials."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from incident_copilot.dashboard import repository_sync
from incident_copilot.dashboard.activities import dashboard_stage_activity
from incident_copilot.dashboard.config import Configuration
from incident_copilot.dashboard.models import (
    IncidentSubmission,
    RepositoryProfile,
    RepositoryWrite,
    SettingsUpdate,
)
from incident_copilot.dashboard.repository_source import repository_root
from incident_copilot.dashboard.repository_sync import (
    GitClient,
    SyncFailure,
    incident_index,
    prepare_snapshot,
)
from incident_copilot.dashboard.service import DashboardService
from incident_copilot.dashboard.store import ControlStore
from incident_copilot.indexing.storage import IndexStorage


@pytest.fixture
def service(tmp_path, monkeypatch):
    for key, value in {
        "IC_CONTROL_DB_PATH": tmp_path / "control.db",
        "IC_DATA_DIR": tmp_path / "data",
        "IC_INDEX_DB_PATH": tmp_path / "indexes" / "index.db",
        "IC_REPOSITORY_CACHE_DIR": tmp_path / "cache",
        "IC_TREE_SITTER_ENABLED": "false",
        "IC_CODE_GRAPH_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, str(value))
    return DashboardService(ControlStore(tmp_path / "control.db"))


def github(service, **values):
    value = RepositoryWrite(
        name="Remote",
        source_type="github",
        remote_url="https://github.com/acme/example",
        branch="main",
        **values,
    )
    return service.store.save_repository(
        "remote", value.model_dump(exclude={"github_token", "clear_github_token"})
    )


def git(root, *args):
    result = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def origin(tmp_path):
    root = tmp_path / "origin"
    root.mkdir()
    git(root, "init", "-b", "main")
    (root / "main.py").write_text("def charge():\n    return 1\n", encoding="utf-8")
    commit(root)
    return root


def commit(root):
    git(root, "add", "-A")
    git(
        root,
        "-c",
        "user.name=Sync Test",
        "-c",
        "user.email=sync@example.invalid",
        "commit",
        "-m",
        "source",
    )
    return git(root, "rev-parse", "HEAD")


class FixtureGit:
    def __init__(self, origin):
        self.origin = origin
        self.calls = []

    def run(self, *args, cwd=None, timeout=600):
        self.calls.append(args[0])
        args = [
            str(self.origin) if str(arg).startswith("https://github.com/") else arg for arg in args
        ]
        result = subprocess.run(
            [
                "git",
                "-c",
                "safe.directory=*",
                "-c",
                "core.longpaths=true",
                "-c",
                "core.hooksPath=" + os.devnull,
                *(["-C", str(cwd)] if cwd else []),
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode:
            raise SyncFailure(result.stderr)
        return result.stdout.strip()


def sync(service, origin):
    job = service.request_sync("remote")
    snapshot, unchanged = prepare_snapshot(
        job, service.configuration.effective(), FixtureGit(origin), lambda stage: None
    )
    service.store.publish_repository_sync(job["id"], snapshot, unchanged=unchanged, graph=None)
    return snapshot


def test_first_sync_unchanged_and_changed_revisions(service, origin):
    github(service)
    first = sync(service, origin)
    assert first["revision"] == git(origin, "rev-parse", "HEAD")
    assert IndexStorage(Path(first["index_db_path"])).get_file_record("main.py")
    job = service.request_sync("remote")
    transport = FixtureGit(origin)
    snapshot, unchanged = prepare_snapshot(
        job, service.configuration.effective(), transport, lambda stage: None
    )
    assert unchanged and snapshot == first
    assert transport.calls == ["ls-remote"]
    service.store.publish_repository_sync(job["id"], snapshot, unchanged=True, graph=None)
    (origin / "main.py").unlink()
    (origin / "new.py").write_text("def other(): return 2\n", encoding="utf-8")
    commit(origin)
    second = sync(service, origin)
    assert second["revision"] != first["revision"]
    assert not IndexStorage(Path(second["index_db_path"])).get_file_record("main.py")
    assert IndexStorage(Path(second["index_db_path"])).get_file_record("new.py")
    assert (Path(first["root"]) / "main.py").is_file()


def test_incidents_and_reruns_pin_snapshots_and_copy_indexes(service, origin, monkeypatch):
    github(service)
    with pytest.raises(ValueError, match="Check & sync"):
        service.submit(IncidentSubmission(repository_id="remote", log_text="error"))
    first = sync(service, origin)
    incident = service.submit(IncidentSubmission(repository_id="remote", log_text="error"))
    original = service.store.get("incidents", incident.id)
    (origin / "main.py").write_text("def charge(): return 2\n", encoding="utf-8")
    commit(origin)
    second = sync(service, origin)
    rerun = service.submit(
        IncidentSubmission(repository_id="remote", log_text="error"), original=original
    )
    assert rerun.repository_source["commit"] == first["revision"]
    assert (
        service.submit(
            IncidentSubmission(repository_id="remote", log_text="error")
        ).repository_source["commit"]
        == second["revision"]
    )
    monkeypatch.setattr(
        repository_sync,
        "index_repository",
        lambda *args: pytest.fail("Compatible cache must not be rebuilt"),
    )
    asyncio.run(dashboard_stage_activity(incident.id, "indexing"))
    copied = IndexStorage(Path(original["index_db_path"]))
    assert copied.snapshot_identity() == (first["root"], first["revision"])
    assert original["input"]["repo_root"] != second["root"]


def test_invalid_cache_rebuilds_locally_without_remote_contact(service, origin, monkeypatch):
    github(service)
    snapshot = sync(service, origin)
    incident = service.submit(IncidentSubmission(repository_id="remote", log_text="error"))
    run = service.store.get("incidents", incident.id)
    monkeypatch.setattr(repository_sync, "index_fingerprint", lambda: "new-parser-version")
    monkeypatch.setattr(
        GitClient, "run", lambda *a, **kw: pytest.fail("Analysis must not contact GitHub")
    )
    summary = incident_index(run["input"], Path(run["index_db_path"]))
    assert summary["repo_revision"] == snapshot["revision"]


@pytest.mark.parametrize("change", ["edit", "archive"])
def test_profile_change_prevents_publication(service, origin, change):
    profile = github(service)
    job = service.request_sync("remote")
    snapshot, _ = prepare_snapshot(
        job, service.configuration.effective(), FixtureGit(origin), lambda stage: None
    )
    if change == "edit":
        service.store.save_repository("remote", profile | {"branch": "other"})
    else:
        service.store.update("repositories", "remote", {"archived": True})
    with pytest.raises(ValueError, match="configuration changed"):
        service.store.publish_repository_sync(job["id"], snapshot, unchanged=False, graph=None)
    assert repository_root(service.store.get("repositories", "remote")) is None


def test_duplicate_requests_and_infrastructure_guard(service):
    github(service)
    with ThreadPoolExecutor(max_workers=4) as executor:
        jobs = list(executor.map(lambda _: service.request_sync("remote"), range(4)))
    assert len({job["id"] for job in jobs}) == 1
    service.configuration.save(
        SettingsUpdate(values={"repository_cache_dir": str(service.store.path.parent / "other")})
    )
    assert (
        Configuration(service.store).effective().repository_cache_dir
        == service.configuration.effective().repository_cache_dir
    )
    service.store.update("repository_syncs", jobs[0]["id"], {"status": "failed"})
    assert (
        Configuration(service.store).effective().repository_cache_dir
        != service.configuration.effective().repository_cache_dir
    )


def test_request_recovers_dispatch_without_any_automatic_git(service, monkeypatch):
    github(service)
    monkeypatch.setattr(
        GitClient,
        "run",
        lambda *a, **kw: pytest.fail("Only the requested worker activity may contact GitHub"),
    )
    asyncio.run(service.reconcile())
    assert service.sync_status("remote")["status"] == "idle"
    job = service.request_sync("remote")
    starts = []

    class Gateway:
        async def start_sync(self, job_id):
            starts.append(job_id)

        async def inspect_sync(self, job_id):
            return "RUNNING"

    restarted = DashboardService(service.store)
    restarted.gateway = Gateway()
    asyncio.run(restarted.reconcile())
    asyncio.run(restarted.reconcile())
    assert starts == [job["id"]]
    assert restarted.sync_status("remote")["status"] == "queued"


def test_tokens_are_private_and_blank_preserves_them(service, monkeypatch):
    from apps.api.main import app

    app.state.dashboard = service
    client = TestClient(app)
    token = "github_pat_private_example"
    payload = {
        "name": "Private",
        "source_type": "github",
        "remote_url": "https://github.com/acme/private",
        "branch": "main",
        "github_token": token,
    }
    monkeypatch.setattr(
        GitClient, "run", lambda *a, **kw: pytest.fail("API reads/saves must not contact GitHub")
    )
    response = client.post("/api/repositories", json=payload)
    assert response.status_code == 201 and token not in response.text
    key = response.json()["id"]
    assert response.json()["token_configured"]
    assert token not in json.dumps(service.store.get("repositories", key))
    assert client.put(f"/api/repositories/{key}", json=payload | {"github_token": ""}).json()[
        "token_configured"
    ]
    assert service.store.repository_token(key) == token
    assert token not in client.get("/api/repositories").text
    assert client.get(f"/api/repositories/{key}/sync").json()["status"] == "idle"
    assert client.get(f"/api/repositories/{key}/graph").status_code == 200
    assert (
        client.post("/api/incidents", json={"repository_id": key, "log_text": "error"}).status_code
        == 422
    )
    sync_response = client.post(f"/api/repositories/{key}/sync")
    assert sync_response.status_code == 202 and token not in sync_response.text
    assert token not in json.dumps(service.store.list("repository_syncs"))
    assert (
        token not in client.post("/api/repositories", json=payload | {"remote_url": "invalid"}).text
    )
    assert not client.put(
        f"/api/repositories/{key}", json=payload | {"github_token": "", "clear_github_token": True}
    ).json()["token_configured"]
    assert service.store.repository_token(key) == ""


@pytest.mark.parametrize(
    "values",
    [
        {"remote_url": "https://evil.example/acme/repo"},
        {"remote_url": "https://token@github.com/acme/repo"},
        {"remote_url": "https://github.com/acme/repo?token=secret"},
        {"branch": "--upload-pack=evil"},
        {"branch": "main:other"},
        {"branch": "main~1"},
        {"compose_path": "../compose.yml"},
        {"compose_path": "/etc/config.yml"},
        {"compose_path": "C:\\config.yml"},
    ],
)
def test_rejects_unsafe_source_configuration(values):
    with pytest.raises(ValidationError):
        RepositoryWrite(
            **(
                {
                    "name": "Remote",
                    "source_type": "github",
                    "remote_url": "https://github.com/acme/repo",
                    "branch": "main",
                }
                | values
            )
        )


def test_legacy_local_profile_and_local_sync_rejection(service, origin):
    from apps.api.main import app

    app.state.dashboard = service
    client = TestClient(app)
    response = client.post("/api/repositories", json={"name": "Local", "repo_root": str(origin)})
    assert response.status_code == 201
    assert RepositoryProfile.model_validate(response.json()).source_type == "local"
    assert client.post(f"/api/repositories/{response.json()['id']}/sync").status_code == 422
    assert (
        service.submit(
            IncidentSubmission(repository_id=response.json()["id"], log_text="error")
        ).repository_source
        is None
    )


def test_git_auth_is_environment_only_and_errors_are_sanitized(monkeypatch):
    captured = {}

    def execute(command, **kwargs):
        captured.update(command=command, **kwargs)
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="Authentication failed: private-secret-token"
        )

    monkeypatch.setattr(subprocess, "run", execute)
    with pytest.raises(SyncFailure) as error:
        GitClient("private-secret-token").run("ls-remote", "https://github.com/acme/repo.git")
    assert "private-secret-token" not in str(error.value)
    assert "private-secret-token" not in json.dumps(captured["command"])
    assert captured["env"]["IC_GITHUB_TOKEN"] == "private-secret-token"
    assert captured["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_force_push_reuses_older_snapshot_and_preserves_both_revisions(service, origin):
    github(service)
    first = sync(service, origin)
    (origin / "main.py").write_text("def charge(): return 99\n", encoding="utf-8")
    commit(origin)
    second = sync(service, origin)
    git(origin, "reset", "--hard", first["revision"])
    job = service.request_sync("remote")
    transport = FixtureGit(origin)
    reused, unchanged = prepare_snapshot(
        job, service.configuration.effective(), transport, lambda stage: None
    )
    assert reused == first and not unchanged
    assert transport.calls == ["ls-remote"]
    service.store.publish_repository_sync(job["id"], reused, unchanged=False, graph=None)
    assert (Path(second["root"]) / "main.py").read_text().strip().endswith("99")


def test_archived_github_profile_allows_only_pinned_reruns(service, origin):
    github(service)
    snapshot = sync(service, origin)
    submission = IncidentSubmission(repository_id="remote", log_text="error")
    incident = service.submit(submission)
    original = service.store.get("incidents", incident.id)
    service.store.update("repositories", "remote", {"archived": True})
    with pytest.raises(ValueError, match="active repository"):
        service.submit(submission)
    assert service.submit(submission, original=original).repository_source["commit"] == snapshot["revision"]


def test_activity_failure_preserves_snapshot_and_sanitizes_errors(service, origin, monkeypatch):
    from temporalio.exceptions import ApplicationError

    github(service)
    first = sync(service, origin)
    job = service.request_sync("remote")

    class DeniedGit:
        def run(self, *args, **kwargs):
            raise SyncFailure(
                "GitHub access failed. Check the repository URL and token permissions."
            )

    monkeypatch.setattr(repository_sync, "GitClient", lambda token: DeniedGit())
    with pytest.raises(ApplicationError):
        asyncio.run(repository_sync.sync_repository_activity(job["id"]))
    status = service.sync_status("remote")
    assert status["status"] == "failed" and "access failed" in status["error"]
    assert service.store.get("repositories", "remote")["snapshot"] == first
    assert service.request_sync("remote")["id"] != job["id"]


def test_missing_branch_keeps_published_snapshot(service, origin):
    github(service)
    first = sync(service, origin)
    job = service.request_sync("remote")
    git(origin, "branch", "-m", "renamed")
    with pytest.raises(SyncFailure, match="tracked branch does not exist"):
        prepare_snapshot(
            job, service.configuration.effective(), FixtureGit(origin), lambda stage: None
        )
    assert service.store.get("repositories", "remote")["snapshot"] == first


def test_restart_reuses_prepared_unpublished_checkout_and_graph_failure_is_optional(
    service, origin, monkeypatch
):
    from incident_copilot.schemas.code_graph import GraphPreparation

    github(service)
    service.configuration.save(SettingsUpdate(values={"code_graph_enabled": True}))
    job = service.request_sync("remote")
    prepared, _ = prepare_snapshot(
        job, service.configuration.effective(), FixtureGit(origin), lambda stage: None
    )
    assert repository_root(service.store.get("repositories", "remote")) is None
    monkeypatch.setattr(repository_sync, "GitClient", lambda token: FixtureGit(origin))

    async def unavailable(root, revision, config):
        return GraphPreparation(repository=root, status="unavailable", reason="manager_unavailable")

    monkeypatch.setattr(repository_sync, "ensure_graph_ready", unavailable)
    asyncio.run(repository_sync.sync_repository_activity(job["id"]))
    assert service.store.get("repositories", "remote")["snapshot"]["root"] == prepared["root"]
    assert service.sync_status("remote")["status"] == "completed"
    assert service.sync_status("remote")["graph_preparation"]["status"] == "unavailable"
    assert prepared["root"] not in json.dumps(
        service.public_repository(service.store.get("repositories", "remote")).model_dump()
    )


def test_report_exports_record_original_github_revision(service, origin, monkeypatch):
    from apps.api.main import app
    from incident_copilot.dashboard import activities

    github(service)
    first = sync(service, origin)
    incident = service.submit(IncidentSubmission(repository_id="remote", log_text="error"))
    run = service.store.get("incidents", incident.id)

    async def diagnosis(state, config, **kwargs):
        return {
            "incident_id": state["incident_id"],
            "summary": "Source inspected",
            "candidates": [],
            "hypotheses": [],
        }

    monkeypatch.setattr(activities, "run_diagnosis_graph", diagnosis)
    asyncio.run(dashboard_stage_activity(incident.id, "reasoning"))
    app.state.dashboard = service
    client = TestClient(app)
    exported = client.get(f"/api/incidents/{incident.id}/export?format=json")
    assert exported.status_code == 200
    assert exported.json()["repository_source"]["commit"] == first["revision"]
    markdown = client.get(f"/api/incidents/{incident.id}/export?format=markdown")
    assert first["revision"] in markdown.text
    assert first["root"] not in exported.text
    assert first["revision"] in (Path(run["artifact_dir"]) / "report.md").read_text(
        encoding="utf-8"
    )


@pytest.mark.skipif(
    os.getenv("IC_RUN_TEMPORAL_TESTS") != "1", reason="Requires the Temporal dev server"
)
def test_real_temporal_manual_sync_recovery_and_secret_free_history(service, origin, monkeypatch):
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from incident_copilot.workflows.temporal_workflows import SyncRepositoryWorkflow

    monkeypatch.setattr(repository_sync, "GitClient", lambda token: FixtureGit(origin))

    async def scenario():
        download = service.store.path.parent / "temporal"
        download.mkdir()
        async with await WorkflowEnvironment.start_local(
            dev_server_log_level="error", download_dest_dir=str(download)
        ) as environment:
            monkeypatch.setenv(
                "IC_TEMPORAL_HOST", environment.client.service_client.config.target_host
            )
            current = DashboardService(service.store)
            github(current)
            current.store.save_repository("remote", {}, token="github_pat_temporal_secret")
            job = current.request_sync("remote")
            async with Worker(
                environment.client,
                task_queue=current.configuration.effective().temporal_task_queue,
                workflows=[SyncRepositoryWorkflow],
                activities=[repository_sync.sync_repository_activity],
            ):
                await current.reconcile()
                current.store.update("repository_syncs", job["id"], {"dispatched": False})
                restarted = DashboardService(current.store)
                for _ in range(100):
                    await restarted.reconcile()
                    if restarted.sync_status("remote")["status"] in {"completed", "failed"}:
                        break
                    await asyncio.sleep(0.2)
                assert restarted.sync_status("remote")["status"] == "completed"
                handle = environment.client.get_workflow_handle(f"repository-sync-{job['id']}")
                await handle.result()
                assert "github_pat_temporal_secret" not in str(
                    (await handle.fetch_history()).events
                )
                assert current.store.get("repositories", "remote")["synced_revision"] == git(
                    origin, "rev-parse", "HEAD"
                )

    asyncio.run(scenario())
