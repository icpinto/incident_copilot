"""Real Git fixtures verify deployed source selection and ingestion identity."""

import asyncio
import importlib.util
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from temporalio.exceptions import ApplicationError

from incident_copilot.dashboard.activities import dashboard_stage_activity
from incident_copilot.dashboard.deployed_source import prepare_deployed_snapshot
from incident_copilot.dashboard.deployments import context, observe
from incident_copilot.dashboard.issue_models import IssueFilters
from incident_copilot.dashboard.models import IncidentSubmission
from incident_copilot.dashboard.service import DashboardService
from incident_copilot.dashboard.store import ControlStore, utcnow
from incident_copilot.schemas.deployment import ReleaseMetadata


def git(root, *args):
    return subprocess.run(["git", "-c", "safe.directory=*", "-C", str(root), *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def commit(root):
    git(root, "add", "-A")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "release")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for name, value in {"IC_CONTROL_DB_PATH": tmp_path / "control.db", "IC_DATA_DIR": tmp_path / "data",
                        "IC_INDEX_DB_PATH": tmp_path / "indexes" / "index.db",
                        "IC_REPOSITORY_CACHE_DIR": tmp_path / "cache", "IC_TREE_SITTER_ENABLED": "false",
                        "IC_CODE_GRAPH_ENABLED": "false", "IC_LOG_INGEST_TOKEN": "test-collector"}.items():
        monkeypatch.setenv(name, str(value))
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    (root / "main.py").write_text('def charge():\n    raise ValueError("release A")\n')
    first = commit(root)
    (root / "main.py").write_text('def charge():\n    return "release B"\n')
    second = commit(root)
    service = DashboardService(ControlStore(tmp_path / "control.db"))
    service.store.put("repositories", "repo", {"id": "repo", "name": "App", "source_type": "local",
        "repo_root": str(root), "compose_path": None, "archived": False, "created_at": utcnow()})
    return service, root, first, second


def declared(service, revision, **extra):
    return {"deployment_id": "container-a", "collector_id": "host-a", "project": "app", "service": "api",
            "container_id": "container-a", "environment": "production", "revision": revision,
            "version": "v1", "image_ref": "app:latest", "image_id": "sha256:image-a", "repo_digests": [], **extra}


def submit(service, revision, **extra):
    return service.submit(IncidentSubmission(repository_id="repo", log_text='api | ValueError: release A\nFile "/app/main.py", line 2, in charge', user_hint_service="api"),
                          deployment_context=declared(service, revision, **extra), version_linked=True)


def test_local_declared_commit_survives_advanced_dirty_checkout_and_exports(setup, monkeypatch):
    service, root, first, second = setup
    (root / "main.py").write_text("# dirty local work\n")
    (root / "untracked.py").write_text("# leave me alone\n")
    from incident_copilot.graph.nodes import build_hypotheses
    class Offline:
        async def complete(self, **kwargs):
            raise ConnectionError("offline")
    monkeypatch.setattr(build_hypotheses, "get_client", lambda *args, **kwargs: Offline())
    run = submit(service, first)
    assert run.source_resolution.status == "pending"
    async def stages():
        for stage in ("indexing", "correlating", "analyzing", "reasoning"):
            await dashboard_stage_activity(run.id, stage)
    asyncio.run(stages())
    saved = service.store.get("incidents", run.id)
    assert saved["source_resolution"]["analyzed_commit"] == first
    assert saved["repository_source"]["commit"] == first
    assert "release A" in (Path(saved["input"]["repo_root"]) / "main.py").read_text()
    assert saved["evidence"]["index_summary"]["repo_revision"] == first
    assert saved["evidence"]["candidates"][0]["file_path"] == "main.py"
    assert saved["report"]["source_resolution"]["analyzed_commit"] == first
    assert saved["report"]["deployment_context"]["environment"] == "production"
    markdown = (Path(saved["artifact_dir"]) / "report.md").read_text()
    assert first in markdown and "Declared commit" in markdown and "Registry digests" in markdown
    assert git(root, "rev-parse", "HEAD") == second
    assert (root / "main.py").read_text() == "# dirty local work\n"
    assert (root / "untracked.py").exists()
    assert service.store.get("repositories", "repo").get("snapshot") is None
    rerun = service.submit(IncidentSubmission.model_validate(saved["input"]), original=saved)
    asyncio.run(dashboard_stage_activity(rerun.id, "indexing"))
    assert service.store.get("incidents", rerun.id)["input"]["repo_root"] == saved["input"]["repo_root"]
    assert rerun.error_tracking_status == "duplicate"


@pytest.mark.parametrize("revision", [None, "abc123", "a" * 41, "f" * 40])
def test_missing_invalid_or_unavailable_commit_keeps_logs_and_stops(setup, revision):
    service, _, _, _ = setup
    run = submit(service, revision)
    with pytest.raises(ApplicationError):
        asyncio.run(dashboard_stage_activity(run.id, "indexing"))
    saved = service.store.get("incidents", run.id)
    assert saved["source_resolution"]["status"] == "unavailable"
    assert saved["source_resolution"]["reason"]
    assert saved["evidence"]["raw_log"] and not saved["evidence"].get("candidates")
    assert saved["error_tracking_status"] == "tracked"
    assert service.issues.intelligence(IssueFilters(), "service")["summary"]["occurrences"] == 1


def test_concurrent_preparation_isolated_and_cached(setup):
    service, _, first, _ = setup
    inputs = service.store.get("incidents", submit(service, first).id)["input"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        snapshots = list(pool.map(lambda _: prepare_deployed_snapshot(inputs, service.configuration.effective()), range(2)))
    for snapshot in snapshots:
        assert snapshot["revision"] == first
        assert (Path(snapshot["root"]) / "main.py").is_file()
    assert prepare_deployed_snapshot(inputs, service.configuration.effective())["root"] in [s["root"] for s in snapshots]


def test_changed_cached_source_cannot_be_used_for_correlation(setup):
    service, _, first, _ = setup
    run = submit(service, first)
    asyncio.run(dashboard_stage_activity(run.id, "indexing"))
    snapshot = service.store.get("incidents", run.id)["input"]["repo_root"]
    (Path(snapshot) / "main.py").write_text("# tampered\n")
    with pytest.raises(ApplicationError, match="pinned deployed source changed"):
        asyncio.run(dashboard_stage_activity(run.id, "correlating"))


@pytest.fixture
def client(setup, monkeypatch):
    from apps.api.main import app
    from incident_copilot.dashboard import api
    async def no_background(_):
        await asyncio.sleep(3600)
    monkeypatch.setattr(api, "reconciliation_loop", no_background)
    with TestClient(app) as client:
        yield client


def map_source(client, collector_id="host-a", versioned=True):
    headers = {"Authorization": "Bearer test-collector"}
    assert client.post("/api/collector/inventory", json={"collector_id": collector_id, "projects": {"app": ["api", "db"]}}, headers=headers).status_code == 202
    assert client.put(f"/api/log-sources/app?collector_id={collector_id}", json={"repository_id": "repo", "enabled": True,
        "environment": "production", "versioned_services": ["api"] if versioned else []}).status_code == 200
    return headers


def batch(first, **extra):
    return {"collector_id": "host-a", "project": "app", "service": "api", "container_id": "container-a",
            "batch_id": "window-1", "log_text": "ValueError: release A", "release": {"revision": first, "image_ref": "app:latest"}, **extra}


def test_release_cooldown_and_buffered_logs_use_emitting_container(setup, client):
    _, _, first, second = setup
    headers = map_source(client)
    first_batch = batch(first)
    assert client.post("/api/collector/logs", json=first_batch, headers=headers).json() == {"accepted": 1, "investigations": 1}
    assert client.post("/api/collector/logs", json=first_batch, headers=headers).json()["accepted"] == 0
    assert client.post("/api/collector/logs", json=batch(first, batch_id="window-2"), headers=headers).json()["investigations"] == 0
    assert client.post("/api/collector/logs", json=batch(second, container_id="container-b", batch_id="window-3"), headers=headers).json()["investigations"] == 1
    # A delayed window from A still belongs to A after B has appeared.
    assert client.post("/api/collector/logs", json=batch(first, batch_id="delayed"), headers=headers).json()["accepted"] == 1
    runs = client.get("/api/incidents").json()["items"]
    assert {run["deployment_context"]["revision"] for run in runs} == {first, second}
    issues = client.get("/api/issues").json()["items"]
    assert len(issues) == 1 and issues[0]["total_count"] == 4
    occurrences = client.get(f"/api/issues/{issues[0]['id']}/occurrences").json()["items"]
    assert sum(o["event"]["deployment_context"]["revision"] == first for o in occurrences) == 3
    result = client.get("/api/deployments?repository_id=repo&environment=production&service=api&page_size=1").json()
    assert result["total"] == 2 and len(result["items"]) == 1
    assert client.get("/api/deployments?page=0").status_code == 422
    # Inventory refresh cannot rewrite captured incident context.
    client.post("/api/collector/inventory", json={"collector_id": "host-a", "projects": {}, "containers": [
        {"project": "app", "service": "api", "container_id": "container-a", "release": {"revision": second}}]}, headers=headers)
    assert next(run for run in client.get("/api/incidents").json()["items"] if run["deployment_context"]["container_id"] == "container-a")["deployment_context"]["revision"] == first


def test_collector_identity_and_mapping_are_scoped(setup, client):
    _, _, first, _ = setup
    headers = map_source(client)
    map_source(client, "host-b")
    for collector in ("host-a", "host-b"):
        response = client.post("/api/collector/logs", json=batch(first, collector_id=collector), headers=headers)
        assert response.json() == {"accepted": 1, "investigations": 1}
    assert len(client.get("/api/log-sources").json()) == 2
    assert client.get("/api/incidents").json()["total"] == 2


def test_runtime_configuration_is_captured_for_analysis_and_frozen(setup, client, monkeypatch):
    service, _, first, _ = setup
    headers = map_source(client)
    config = {"status": "available", "environment": [
        {"name": "LOG_LEVEL", "value": "debug"},
        {"name": "DB_PASSWORD", "value": "[redacted]"}],
        "mounts": [{"destination": "/app/config", "type": "bind", "read_only": True}],
        "truncated": False}
    observation = {"project": "app", "service": "api", "container_id": "container-a",
                   "release": {"revision": first}, "runtime_config": config}
    assert client.post("/api/collector/inventory", json={"collector_id": "host-a", "projects": {},
        "containers": [observation]}, headers=headers).status_code == 202
    assert client.post("/api/collector/logs", json=batch(first), headers=headers).json()["investigations"] == 1
    run = client.get("/api/incidents").json()["items"][0]
    assert run["deployment_context"]["runtime_config"] == config
    from incident_copilot.dashboard import activities
    from incident_copilot.graph.nodes.build_hypotheses import build_diagnosis_prompt
    from incident_copilot.llm.prompts import DIAGNOSIS_USER_TEMPLATE
    assert "{runtime_config}" in DIAGNOSIS_USER_TEMPLATE
    assert '"LOG_LEVEL": "debug"' not in build_diagnosis_prompt({"parsed_log": {},
        "runtime_config": config})
    assert '"name": "LOG_LEVEL"' in build_diagnosis_prompt({"parsed_log": {},
        "runtime_config": config})
    async def diagnose(payload, settings, runtime_context):
        assert payload["runtime_config"] == config
        return payload | {"summary": "configuration recorded", "engine": "deterministic"}
    monkeypatch.setattr(activities, "run_diagnosis_graph", diagnose)
    for stage in ("indexing", "correlating", "analyzing", "reasoning"):
        asyncio.run(dashboard_stage_activity(run["id"], stage))
    saved = service.store.get("incidents", run["id"])
    assert saved["report"]["deployment_context"]["runtime_config"] == config
    assert "Observed Runtime Configuration" in (Path(saved["artifact_dir"]) / "report.md").read_text()
    changed = observation | {"runtime_config": config | {"environment": [{"name": "LOG_LEVEL", "value": "error"}]}}
    client.post("/api/collector/inventory", json={"collector_id": "host-a", "projects": {},
        "containers": [changed]}, headers=headers)
    assert service.store.get("incidents", run["id"])["deployment_context"]["runtime_config"] == config
    rerun = service.submit(IncidentSubmission.model_validate(saved["input"]), original=saved)
    assert rerun.deployment_context.runtime_config.environment[0].value == "debug"
    assert client.post("/api/collector/inventory", json={"collector_id": "host-a", "projects": {},
        "containers": [observation | {"runtime_config": config | {"environment": [
            {"name": "DB_PASSWORD", "value": "real-secret"}]}}]}, headers=headers).status_code == 422


def test_manual_selection_validates_mapping_and_deduplicates_by_deployment(setup, client):
    _, _, first, second = setup
    headers = map_source(client)
    for revision, container in ((first, "a"), (second, "b")):
        client.post("/api/collector/inventory", json={"collector_id": "host-a", "projects": {}, "containers": [
            {"project": "app", "service": "api", "container_id": container, "release": {"revision": revision}}]}, headers=headers)
    deployments = client.get("/api/deployments").json()["items"]
    payload = {"repository_id": "repo", "log_text": "ValueError: identical upload", "deployment_id": deployments[0]["deployment_id"]}
    assert client.post("/api/incidents", json=payload | {"user_hint_service": "db"}).status_code == 422
    assert client.post("/api/incidents", json=payload | {"deployment_id": "missing"}).status_code == 422
    one = client.post("/api/incidents", json=payload).json()
    two = client.post("/api/incidents", json=payload).json()
    three = client.post("/api/incidents", json=payload | {"deployment_id": deployments[1]["deployment_id"]}).json()
    assert one["error_batch_id"] == two["error_batch_id"] != three["error_batch_id"]
    assert one["source_resolution"]["status"] == "pending"


def test_observations_enrich_identity_without_mutating_frozen_context(setup):
    service, _, first, _ = setup
    with service.store.connection() as db:
        observation = observe(db, collector_id="host-a", project="app", service="api", container_id="a",
                              release=ReleaseMetadata(revision=first), now=utcnow())
        frozen = context(observation, {"environment": "production"})
        observe(db, collector_id="host-a", project="app", service="api", container_id="a",
                release=ReleaseMetadata(image_id="sha256:config", repo_digests=["app@sha256:manifest"]), now=utcnow())
    assert frozen["image_id"] is None and frozen["repo_digests"] == []


def test_persistent_collector_identity_and_inventory_image_digests(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    def load(name, file):
        spec = importlib.util.spec_from_file_location(name, root / "collector" / file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    start = load("collector_start", "start.py")
    monkeypatch.delenv("IC_COLLECTOR_ID", raising=False)
    identity = start.collector_identity(tmp_path)
    assert start.collector_identity(tmp_path) == identity
    monkeypatch.setenv("IC_COLLECTOR_ID", "explicit-host")
    assert start.collector_identity(tmp_path) == "explicit-host"
    inventory = load("collector_inventory", "inventory.py")
    def fetch(path):
        if path == "/containers/json?all=0":
            return [{"Id": "a", "ImageID": "sha256:config", "Image": "app:latest", "Labels": {
                "com.docker.compose.project": "app", "com.docker.compose.service": "api",
                "org.opencontainers.image.revision": "a" * 40}}]
        if path.startswith("/containers/"):
            return {"Config": {"Env": []}, "Mounts": []}
        return {"RepoDigests": ["app@sha256:manifest"]}
    result = inventory.inventory(fetch)
    assert result["collector_id"] == "explicit-host"
    assert result["containers"][0]["release"]["image_id"] == "sha256:config"
    assert result["containers"][0]["release"]["repo_digests"] == ["app@sha256:manifest"]


def test_collector_runtime_config_redacts_values_and_omits_mount_sources(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("collector_runtime_inventory", root / "collector" / "inventory.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def fetch(path):
        if path == "/containers/json?all=0":
            return [{"Id": "container-a", "ImageID": "sha256:image", "Image": "app:v1", "Labels": {
                "com.docker.compose.project": "app", "com.docker.compose.service": "api"}}]
        if path.startswith("/images/"):
            return {"RepoDigests": []}
        return {"Config": {"Env": ["LOG_LEVEL=debug", "DB_PASSWORD=do-not-send", "DB_URL=postgres://user:password@db/app"]},
                "Mounts": [{"Destination": "/app/config", "Source": "/host/private/config",
                            "Type": "bind", "RW": False}]}
    snapshot = module.inventory(fetch)["containers"][0]["runtime_config"]
    assert snapshot["environment"] == [{"name": "LOG_LEVEL", "value": "debug"},
                                       {"name": "DB_PASSWORD", "value": "[redacted]"},
                                       {"name": "DB_URL", "value": "[redacted]"}]
    assert snapshot["mounts"] == [{"destination": "/app/config", "type": "bind", "read_only": True}]
    assert "do-not-send" not in str(snapshot) and "/host/private/config" not in str(snapshot)


def test_github_exact_commit_ignores_latest_synced_branch_and_untrusted_label(setup, monkeypatch):
    service, root, first, second = setup
    from incident_copilot.dashboard import deployed_source
    from incident_copilot.dashboard.repository_source import source_identity
    from incident_copilot.dashboard.repository_sync import GitClient
    profile = service.store.get("repositories", "repo") | {"source_type": "github", "repo_root": None,
                "remote_url": "https://github.com/acme/app.git", "branch": "main"}
    profile["snapshot"] = {"revision": second, "root": str(root), "source_identity": source_identity(profile)}
    service.store.put("repositories", "repo", profile)
    calls = []
    def transport(token="", **kwargs):
        client = GitClient(token, allow_local=True)
        original = client.run
        def run(*args, **options):
            calls.append(args)
            return original(*(str(root) if arg == profile["remote_url"] else arg for arg in args), **options)
        client.run = run
        return client
    monkeypatch.setattr(deployed_source, "GitClient", transport)
    run = submit(service, first, source_url="https://github.com/acme/app")
    asyncio.run(dashboard_stage_activity(run.id, "indexing"))
    saved = service.store.get("incidents", run.id)
    assert saved["repository_source"]["commit"] == first
    assert service.store.get("repositories", "repo")["snapshot"]["revision"] == second
    assert any(args[0] == "fetch" and first in args for args in calls)
    calls.clear()
    bad = submit(service, first, source_url="https://github.com/untrusted/other")
    with pytest.raises(ApplicationError, match="differs from the configured repository"):
        asyncio.run(dashboard_stage_activity(bad.id, "indexing"))
    assert calls == []
    # Exact linking also works before the first normal branch sync.
    profile.pop("snapshot")
    service.store.put("repositories", "repo", profile)
    fresh = submit(service, first)
    asyncio.run(dashboard_stage_activity(fresh.id, "indexing"))
    assert service.store.get("incidents", fresh.id)["source_resolution"]["analyzed_commit"] == first


def test_transient_source_failure_can_resume_and_rerun_same_revision(setup, monkeypatch):
    service, _, first, second = setup
    from incident_copilot.dashboard import deployed_source
    from incident_copilot.dashboard.repository_sync import SyncFailure
    original = deployed_source.prepare_deployed_snapshot
    def offline(*args, **kwargs):
        raise SyncFailure("Temporary source connectivity failure", transient=True)
    monkeypatch.setattr(deployed_source, "prepare_deployed_snapshot", offline)
    run = submit(service, first)
    with pytest.raises(ApplicationError) as failure:
        asyncio.run(dashboard_stage_activity(run.id, "indexing"))
    assert failure.value.non_retryable is False
    failed = service.store.get("incidents", run.id)
    assert failed["source_resolution"]["requested_commit"] == first
    monkeypatch.setattr(deployed_source, "prepare_deployed_snapshot", original)
    restarted = DashboardService(service.store)
    rerun = restarted.submit(IncidentSubmission.model_validate(failed["input"]), original=failed)
    asyncio.run(dashboard_stage_activity(rerun.id, "indexing"))
    assert service.store.get("incidents", rerun.id)["source_resolution"]["analyzed_commit"] == first != second


def test_inventory_enrichment_does_not_reset_release_cooldown(setup, client):
    _, _, first, _ = setup
    headers = map_source(client)
    assert client.post("/api/collector/logs", json=batch(first), headers=headers).json()["investigations"] == 1
    client.post("/api/collector/inventory", json={"collector_id": "host-a", "projects": {}, "containers": [
        {"project": "app", "service": "api", "container_id": "container-a", "release": {
            "revision": first, "image_id": "sha256:now-known", "repo_digests": ["app@sha256:digest"]}}]}, headers=headers)
    assert client.post("/api/collector/logs", json=batch(first, batch_id="next"), headers=headers).json()["investigations"] == 0


def test_inventory_migration_retains_legacy_mapping_and_defaults(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    with store.connection() as db:
        db.execute("INSERT INTO log_sources VALUES (?,?,?,?,?)", ("legacy-app", '["api"]', "repo", 1, utcnow()))
    migrated = ControlStore(store.path)
    with migrated.connection() as db:
        row = db.execute("SELECT * FROM collector_sources WHERE project='legacy-app'").fetchone()
    assert row["collector_id"] == "legacy" and row["enabled"] == 1
    assert row["versioned_services"] == "[]" and row["environment"] == "unknown"


def test_atomic_ingestion_retries_after_capture_failure(setup, client, monkeypatch):
    _, _, first, _ = setup
    headers = map_source(client)
    from incident_copilot.dashboard import issue_capture
    original = issue_capture.capture_upload
    def broken(*args, **kwargs):
        raise RuntimeError("simulated database interruption")
    monkeypatch.setattr(issue_capture, "capture_upload", broken)
    with pytest.raises(RuntimeError, match="database interruption"):
        client.post("/api/collector/logs", json=batch(first), headers=headers)
    monkeypatch.setattr(issue_capture, "capture_upload", original)
    assert client.post("/api/collector/logs", json=batch(first), headers=headers).json() == {"accepted": 1, "investigations": 1}
    assert client.get("/api/incidents").json()["total"] == 1


@pytest.mark.skipif(os.getenv("IC_RUN_TEMPORAL_TESTS") != "1", reason="Requires the Temporal dev server")
def test_temporal_deployed_source_and_missing_metadata(setup, tmp_path, monkeypatch):
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from incident_copilot.graph.nodes import build_hypotheses
    from incident_copilot.workflows.temporal_workflows import DiagnoseIncidentWorkflow
    service, _, first, _ = setup
    class Offline:
        async def complete(self, **kwargs):
            raise ConnectionError("offline")
    monkeypatch.setattr(build_hypotheses, "get_client", lambda *args, **kwargs: Offline())
    async def scenario():
        download = tmp_path / "temporal"
        download.mkdir()
        async with await WorkflowEnvironment.start_local(dev_server_log_level="error", download_dest_dir=str(download)) as env:
            monkeypatch.setenv("IC_TEMPORAL_HOST", env.client.service_client.config.target_host)
            restarted = DashboardService(service.store)
            async with Worker(env.client, task_queue=restarted.configuration.effective().temporal_task_queue,
                              workflows=[DiagnoseIncidentWorkflow], activities=[dashboard_stage_activity]):
                valid = submit(restarted, first)
                invalid = submit(restarted, None)
                await restarted.reconcile()
                for _ in range(100):
                    await restarted.reconcile()
                    records = [service.store.get("incidents", run.id) for run in (valid, invalid)]
                    if all(run["status"] in {"completed", "failed"} for run in records):
                        break
                    await asyncio.sleep(.2)
                assert records[0]["status"] == "completed", records[0]
                assert records[0]["report"]["source_resolution"]["analyzed_commit"] == first
                assert records[1]["status"] == "failed", records[1]
                assert "Missing or invalid deployed commit" in records[1]["error"]
    asyncio.run(scenario())


def test_graph_and_evidence_tools_use_the_deployed_snapshot(setup, monkeypatch):
    service, root, first, second = setup
    from incident_copilot.analysis.evidence_tools import EvidenceTools
    from incident_copilot.dashboard import activities
    from incident_copilot.schemas.investigation import EvidenceRequest
    run = submit(service, first)
    asyncio.run(dashboard_stage_activity(run.id, "indexing"))
    asyncio.run(dashboard_stage_activity(run.id, "correlating"))
    saved = service.store.get("incidents", run.id)
    deployed_root = Path(saved["input"]["repo_root"])
    assert deployed_root != root and git(root, "rev-parse", "HEAD") == second
    tools = EvidenceTools(deployed_root, Path(saved["index_db_path"]), service.configuration.effective(), {"repository": str(deployed_root), "revision": first})
    evidence = asyncio.run(tools.execute(EvidenceRequest(tool="read_source", question="What did the deployed code do?",
        file_path="main.py", line_start=1, line_end=2), "read-deployed"))
    assert evidence.status == "ok"
    assert all(item.revision == first for item in evidence.evidence)
    assert any("release A" in item.content for item in evidence.evidence)
    async def graph(repo_root, index_path, candidates, config, **kwargs):
        assert repo_root == deployed_root
        assert Path(index_path) == Path(saved["index_db_path"])
        return {"repository": str(repo_root), "status": "available", "repo_revision": first, "indexed_revision": first}
    monkeypatch.setattr(activities, "retrieve_graph_context", graph)
    asyncio.run(dashboard_stage_activity(run.id, "analyzing"))
    async def diagnose(payload, config, runtime_context):
        assert runtime_context.repo_root == deployed_root
        assert runtime_context.index_db_path == Path(saved["index_db_path"])
        return payload | {"summary": "Verified source selection", "engine": "deterministic",
                          "retrieved_evidence": [item.model_dump() for item in evidence.evidence]}
    monkeypatch.setattr(activities, "run_diagnosis_graph", diagnose)
    asyncio.run(dashboard_stage_activity(run.id, "reasoning"))
    report = service.store.get("incidents", run.id)["report"]
    assert report["graph_context"]["indexed_revision"] == first
    assert all(item["revision"] == first for item in report["retrieved_evidence"])


def test_deployed_snapshot_can_be_published_by_normal_sync(setup, monkeypatch):
    service, root, _, second = setup
    from incident_copilot.dashboard import deployed_source
    from incident_copilot.dashboard.repository_sync import GitClient, prepare_snapshot
    profile = service.store.get("repositories", "repo") | {"source_type": "github", "repo_root": None,
                "remote_url": "https://github.com/acme/app.git", "branch": "main"}
    service.store.put("repositories", "repo", profile)
    def transport(token="", **kwargs):
        client = GitClient(token, allow_local=True)
        original = client.run
        def run(*args, **options):
            return original(*(str(root) if arg == profile["remote_url"] else arg for arg in args), **options)
        client.run = run
        return client
    monkeypatch.setattr(deployed_source, "GitClient", transport)
    run = submit(service, second)
    inputs = service.store.get("incidents", run.id)["input"]
    prepared = prepare_deployed_snapshot(inputs, service.configuration.effective())
    job = service.request_sync("repo")
    snapshot, unchanged = prepare_snapshot(job, service.configuration.effective(), transport(), lambda stage: None)
    assert snapshot["root"] == prepared["root"]
    service.store.publish_repository_sync(job["id"], snapshot, unchanged=unchanged, graph=None)
    assert service.store.get("repositories", "repo")["synced_revision"] == second
    assert service.store.get("repositories", "repo")["validation"]["valid"]
