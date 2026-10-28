from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from incident_copilot.dashboard.activities import dashboard_stage_activity, validate_repository
from incident_copilot.dashboard.config import Configuration
from incident_copilot.dashboard.models import IncidentSubmission, SettingsUpdate
from incident_copilot.dashboard.service import DashboardService
from incident_copilot.dashboard.store import ControlStore, utcnow


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("IC_CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("IC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("IC_INDEX_DB_PATH", str(tmp_path / "indexes" / "repo.db"))
    monkeypatch.setenv("IC_LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("IC_OPENAI_COMPATIBLE_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("IC_OLLAMA_BASE_URL", "http://127.0.0.1:1")
    return DashboardService(ControlStore(tmp_path / "control.db"))


def profile(service, root: Path, key="repo"):
    value = {"id": key, "name": key, "repo_root": str(root), "compose_path": None,
             "archived": False, "created_at": utcnow(), "validation": None, "index_summary": None}
    service.store.put("repositories", key, value)
    return value


def submit(service, root: Path, key="repo"):
    profile(service, root, key)
    return service.submit(IncidentSubmission(repository_id=key, title="Payment timeout", log_text='api | ValueError: charge failed\nFile "/app/main.py", line 1, in charge', user_hint_service="api"))


def test_configuration_override_reset_redaction_and_validation(service, monkeypatch):
    monkeypatch.setenv("IC_TOP_K_CANDIDATES", "15")
    config = service.configuration
    config.save(SettingsUpdate(values={"top_k_candidates": 8, "openai_compatible_api_key": "private-test-key"}))
    assert config.effective().top_k_candidates == 8
    view = config.view()
    assert view.secret_configured and "private-test-key" not in view.model_dump_json()
    revision = config.revision()
    assert service.store.get("revisions", revision)["openai_compatible_api_key"] == "private-test-key"
    config.save(SettingsUpdate(values={"openai_compatible_api_key": "replacement-key"}))
    assert config.redact({"error": "private-test-key replacement-key"}) == {"error": "[redacted] [redacted]"}
    config.save(SettingsUpdate(reset=["top_k_candidates"]))
    assert config.effective().top_k_candidates == 15
    with pytest.raises(ValueError):
        config.save(SettingsUpdate(values={"deep_analysis_top_n": 20}))
    with pytest.raises(ValueError):
        config.save(SettingsUpdate(values={"llm_provider": "unknown"}))
    with pytest.raises(ValueError):
        config.save(SettingsUpdate(values={"ollama_base_url": "http://secret:password@localhost"}))


def test_infrastructure_activation_waits_for_active_runs(service, tmp_path):
    original = service.configuration.effective().temporal_host
    run = submit(service, tmp_path)
    service.configuration.save(SettingsUpdate(values={"temporal_host": "new-temporal:7233"}))
    assert service.configuration.view().restart_required == ["temporal_host"]
    restarted = Configuration(service.store)
    assert restarted.effective().temporal_host == original
    service.store.update("incidents", run.id, {"status": "completed"})
    activated = Configuration(service.store)
    assert activated.effective().temporal_host == "new-temporal:7233"
    # The still-running API retains its process-local active values until restarted.
    assert service.configuration.effective().temporal_host == original


def test_queued_incident_can_be_canceled_to_unblock_infrastructure_recovery(service, tmp_path):
    run = submit(service, tmp_path)
    service.configuration.save(SettingsUpdate(values={"temporal_host": "fixed:7233"}))
    canceled = asyncio.run(service.cancel(run.id))
    assert canceled.status == "canceled"
    assert Configuration(service.store).effective().temporal_host == "fixed:7233"
    with pytest.raises(ValueError, match="Restart both"):
        submit(service, tmp_path)


def test_submission_persists_before_dispatch_and_recovers(service, tmp_path):
    run = submit(service, tmp_path)
    assert service.store.get("incidents", run.id)["dispatched"] is False

    class Offline:
        async def start(self, key):
            raise ConnectionError("offline")
    service.gateway = Offline()
    asyncio.run(service.reconcile())
    assert service.store.get("incidents", run.id)["connection_error"]
    recovered = DashboardService(service.store)

    class Online:
        def __init__(self):
            self.started = []
        async def start(self, key):
            self.started.append(key)
        async def inspect(self, key):
            return "RUNNING", {"stage": "indexing"}
    gateway = recovered.gateway = Online()
    asyncio.run(recovered.reconcile())
    asyncio.run(recovered.reconcile())
    assert gateway.started == [run.id]
    assert recovered.store.get("incidents", run.id)["stage"] == "indexing"
    assert recovered.store.get("incidents", run.id)["connection_error"] is None


def test_failure_reconciliation_retains_evidence(service, tmp_path):
    run = submit(service, tmp_path)
    service.store.update("incidents", run.id, {"dispatched": True, "stage": "reasoning", "timeline": [{"stage": "reasoning", "started_at": utcnow(), "attempt": 3}]})
    class Failed:
        async def inspect(self, key):
            return "FAILED", None
    service.gateway = Failed()
    asyncio.run(service.reconcile())
    result = service.public(service.store.get("incidents", run.id))
    assert result.status == "failed" and result.completed_at
    assert result.timeline[0].attempt == 3 and "reasoning" in result.error
    assert result.evidence.raw_log


def test_worker_validation_handles_paths_and_compose(tmp_path):
    assert not validate_repository("relative/path", None)["valid"]
    assert not validate_repository(str(tmp_path), "missing.yml")["valid"]
    compose = tmp_path / "compose.yml"
    compose.write_text("services:\n  api:\n    image: example/api\n")
    result = validate_repository(str(tmp_path), "compose.yml")
    assert result["valid"] and result["services"][0]["service_name"] == "api"
    compose.write_text("services: [invalid")
    assert not validate_repository(str(tmp_path), "compose.yml")["valid"]


def test_concurrent_repositories_and_configuration_snapshots(service, tmp_path, monkeypatch):
    # Force deterministic fallback without calling an external model.
    from incident_copilot.graph.nodes import build_hypotheses
    class Unavailable:
        async def complete(self, **kwargs):
            raise ConnectionError("unavailable")
    monkeypatch.setattr(build_hypotheses, "get_client", lambda *_: Unavailable())
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir(); two.mkdir()
    (one / "main.py").write_text("def charge():\n    raise ValueError('charge failed')\n")
    (two / "worker.py").write_text("def work():\n    raise ValueError('charge failed')\n")
    first = submit(service, one, "first")
    service.configuration.save(SettingsUpdate(values={"top_k_candidates": 7}))
    second = submit(service, two, "second")
    assert first.config_revision != second.config_revision
    assert service.store.get("incidents", first.id)["index_db_path"] != service.store.get("incidents", second.id)["index_db_path"]

    async def run(key):
        for stage in ("indexing", "correlating", "analyzing", "reasoning"):
            await dashboard_stage_activity(key, stage)
    async def together():
        await asyncio.gather(run(first.id), run(second.id))
    asyncio.run(together())
    a = service.store.get("incidents", first.id)
    b = service.store.get("incidents", second.id)
    assert a["report"]["engine"] == b["report"]["engine"] == "deterministic"
    assert [c["file_path"] for c in a["report"]["candidates"]] == ["main.py"]
    assert all(c["file_path"] == "worker.py" for c in b["report"]["candidates"])
    assert len(a["timeline"]) == 4 and all(e["completed_at"] for e in a["timeline"])
    assert a["evidence"]["parsed_log"]["services"][0] == "api"


@pytest.fixture
def client(service, monkeypatch):
    import incident_copilot.dashboard.api as dashboard_api
    from apps.api.main import app
    async def no_background(_):
        await asyncio.sleep(3600)
    monkeypatch.setattr(dashboard_api, "reconciliation_loop", no_background)
    with TestClient(app) as client:
        yield client


def test_api_contract_filters_exports_and_legacy_health(client, tmp_path):
    assert client.get("/health").json() == {"status": "ok"}
    repo = client.post("/api/repositories", json={"name": "Commerce", "repo_root": str(tmp_path)}).json()
    result = client.post("/api/incidents", json={"repository_id": repo["id"], "title": "Checkout", "log_text": "ValueError: timeout"})
    assert result.status_code == 202
    run = result.json()
    assert "input" not in run and "index_db_path" not in run
    assert client.get("/api/incidents?q=checkout&status=queued").json()["total"] == 1
    assert client.get("/api/incidents?error_class=TypeError").json()["total"] == 0
    assert client.get("/api/incidents?start_date=2099-01-01").json()["total"] == 0
    assert client.get("/api/incidents?page=0").status_code == 422
    assert client.get(f"/api/incidents/{run['id']}/export").status_code == 409
    assert client.post(f"/api/incidents/{run['id']}/rerun").status_code == 202
    assert client.post("/api/incidents", json={"repository_id": repo["id"], "log_text": "  "}).status_code == 422
    assert client.get("/api/missing").status_code == 404
    assert client.post("/api/repositories", json={"name": "Bad", "repo_root": "/tmp"}, headers={"Origin": "https://unrelated.example"}).status_code == 403
    schema = client.get("/openapi.json").json()
    assert schema["paths"]["/diagnose"]["post"]["operationId"] == "post_diagnose"


def test_settings_errors_do_not_echo_credentials(client):
    secret = "should-never-appear"
    response = client.put("/api/settings", json={"values": {"openai_compatible_api_key": secret, "top_k_candidates": "invalid"}})
    assert response.status_code == 422 and secret not in response.text
    response = client.put("/api/settings", json={"values": {"openai_compatible_api_key": secret}})
    assert response.status_code == 200 and secret not in response.text


@pytest.mark.parametrize("status", [200, 401, 503])
def test_gateway_connection_check_uses_saved_endpoint_and_redacts_key(client, monkeypatch, status):
    key = "sk-private-gateway-check"
    saved = client.put("/api/settings", json={"values": {
        "llm_provider": "openai_compatible",
        "openai_compatible_base_url": "http://litellm:4000/v1/",
        "openai_compatible_model": "incident-diagnosis",
        "openai_compatible_api_key": key,
    }})
    assert saved.status_code == 200 and key not in saved.text
    requests = []

    def handle(request):
        requests.append(request)
        assert str(request.url) == "http://litellm:4000/v1/models"
        assert request.headers["Authorization"] == f"Bearer {key}"
        return httpx.Response(status, json={"data": [{"id": "incident-diagnosis"}], "error": key})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handle), **kwargs))
    result = client.post("/api/connections/check", json={"target": "llm"})
    assert result.status_code == 200
    assert result.json()["ok"] is (status == 200)
    assert key not in result.text
    assert len(requests) == 1


def test_redaction_preserves_structural_status_fields(service, tmp_path):
    service.configuration.save(SettingsUpdate(values={"openai_compatible_api_key": "queued"}))
    run = submit(service, tmp_path)
    assert run.status == "queued" and run.stage == "queued"
    assert service.configuration.redact({"message": "Token queued appeared"})["message"] == "Token [redacted] appeared"


def test_graph_status_and_refresh_use_profile_path_and_saved_mode(client, tmp_path, monkeypatch):
    import incident_copilot.dashboard.api as dashboard_api
    from incident_copilot.schemas.code_graph import GraphPreparation
    calls = []
    async def status(repository, config, *, refresh=False):
        calls.append((repository, config.code_graph_mode, refresh))
        return GraphPreparation(repository=repository, status="preparing" if refresh else "not_prepared")
    monkeypatch.setattr(dashboard_api, "repository_graph_status", status)
    repo = client.post("/api/repositories", json={"name": "Graph repository", "repo_root": str(tmp_path)}).json()
    assert client.get(f"/api/repositories/{repo['id']}/graph").json()["status"] == "not_prepared"
    refreshed = client.post(f"/api/repositories/{repo['id']}/graph/refresh")
    assert refreshed.status_code == 202 and refreshed.json()["status"] == "preparing"
    assert calls == [(str(tmp_path), "managed", False), (str(tmp_path), "managed", True)]
    assert client.get("/api/repositories/missing/graph").status_code == 404
    client.post(f"/api/repositories/{repo['id']}/archive")
    assert client.post(f"/api/repositories/{repo['id']}/graph/refresh").status_code == 422


def test_graph_defaults_can_be_disabled_and_external_mode_is_validated(client):
    settings = client.get("/api/settings").json()
    assert settings["saved"]["code_graph_enabled"] is True
    assert settings["saved"]["code_graph_mode"] == "managed"
    response = client.put("/api/settings", json={"values": {"code_graph_enabled": False, "code_graph_mode": "external"}})
    assert response.status_code == 200 and response.json()["saved"]["code_graph_enabled"] is False
    assert response.json()["restart_required"] == []
    assert client.put("/api/settings", json={"values": {"code_graph_mode": "unknown"}}).status_code == 422
    assert client.put("/api/settings", json={"values": {"code_graph_prepare_timeout_seconds": 301}}).status_code == 422


def test_exports_redact_credentials_and_preserve_report_contract(client, tmp_path):
    service = client.app.state.dashboard
    service.configuration.save(SettingsUpdate(values={"openai_compatible_api_key": "export-secret"}))
    run = submit(service, tmp_path)
    report = {"incident_id": run.id, "likely_service": "api", "likely_error_class": "ValueError", "candidates": [], "hypotheses": [], "summary": "Error included export-secret", "engine": "deterministic"}
    service.store.update("incidents", run.id, {"status": "completed", "report": report})
    response = client.get(f"/api/incidents/{run.id}/export")
    assert response.status_code == 200 and "export-secret" not in response.text
    assert response.json()["engine"] == "deterministic"
    markdown = client.get(f"/api/incidents/{run.id}/export?format=markdown")
    assert markdown.status_code == 200 and "export-secret" not in markdown.text
    assert ".md" in markdown.headers["Content-Disposition"]


def test_legacy_synchronous_diagnosis_uses_limits_and_fallback(client, tmp_path, monkeypatch):
    from incident_copilot.graph.nodes import build_hypotheses
    class Unavailable:
        async def complete(self, **kwargs):
            raise ConnectionError("offline")
    monkeypatch.setattr(build_hypotheses, "get_client", lambda *_: Unavailable())
    for index in range(9):
        (tmp_path / f"payment_{index}.py").write_text("def charge():\n    raise ValueError('payment')\n")
    client.put("/api/settings", json={"values": {"top_k_candidates": 7}})
    response = client.post("/diagnose", json={"repo_root": str(tmp_path), "log_text": 'api | ValueError: payment charge failed', "user_hint_service": "api"})
    assert response.status_code == 200, response.text
    report = response.json()
    assert len(report["candidates"]) == 7
    assert report["engine"] == "deterministic" and report["likely_service"] == "api"


def test_reindex_replaces_stale_files_and_ignores_generated_directories(tmp_path):
    from incident_copilot.indexing.repo_indexer import index_repository
    from incident_copilot.indexing.storage import IndexStorage
    root = tmp_path / "source"
    root.mkdir()
    (root / "main.py").write_text("def main(): pass")
    (root / ".venv").mkdir()
    (root / ".venv" / "ignored.py").write_text("def ignored(): pass")
    index = tmp_path / "index.db"
    first = index_repository(root, None, index)
    assert first.indexed_files == 1
    (root / "main.py").unlink()
    (root / "replacement.py").write_text("def replacement(): pass")
    index_repository(root, None, index)
    assert [record.file_path for record in IndexStorage(index).get_file_records()] == ["replacement.py"]
