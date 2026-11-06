import asyncio

from fastapi.testclient import TestClient

from incident_copilot.dashboard.issue_models import IssueFilters


def test_collector_mapping_retry_and_cooldown(tmp_path, monkeypatch):
    import incident_copilot.dashboard.api as dashboard_api
    from apps.api.main import app

    monkeypatch.setenv("IC_CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("IC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("IC_INDEX_DB_PATH", str(tmp_path / "index.db"))
    monkeypatch.setenv("IC_LOG_INGEST_TOKEN", "collector-test-token")

    async def no_background(_):
        await asyncio.sleep(3600)

    monkeypatch.setattr(dashboard_api, "reconciliation_loop", no_background)
    headers = {"Authorization": "Bearer collector-test-token"}
    batch = {"project": "payments", "service": "api", "container_id": "abc",
             "batch_id": "window-1", "log_text": "ValueError: payment failed"}
    with TestClient(app) as client:
        assert client.post("/api/collector/inventory", json={"projects": {"payments": ["api"]}}).status_code == 401
        assert client.post("/api/collector/inventory", json={"projects": {"payments": ["api"]}}, headers=headers).status_code == 202
        assert client.post("/api/collector/logs", json=batch, headers=headers).json()["accepted"] == 0
        repo = client.post("/api/repositories", json={"name": "Payments", "repo_root": str(tmp_path)}).json()
        mapping = {"repository_id": repo["id"], "enabled": True}
        assert client.put("/api/log-sources/payments", json=mapping).status_code == 200
        first = client.post("/api/collector/logs", json=batch, headers=headers)
        assert first.status_code == 202 and first.json() == {"accepted": 1, "investigations": 1}
        assert client.post("/api/collector/logs", json=batch, headers=headers).json()["accepted"] == 0
        second = client.post("/api/collector/logs", json={**batch, "batch_id": "window-2"}, headers=headers)
        assert second.json() == {"accepted": 1, "investigations": 0}
        assert client.get("/api/incidents").json()["total"] == 1
        issues = app.state.dashboard.issues.intelligence(IssueFilters(), "service")
        assert issues["summary"]["occurrences"] == 2


def test_collector_rejects_missing_token(tmp_path, monkeypatch):
    import incident_copilot.dashboard.api as dashboard_api
    from apps.api.main import app

    monkeypatch.setenv("IC_CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.delenv("IC_LOG_INGEST_TOKEN", raising=False)

    async def no_background(_):
        await asyncio.sleep(3600)

    monkeypatch.setattr(dashboard_api, "reconciliation_loop", no_background)
    with TestClient(app) as client:
        assert client.post("/api/collector/inventory", json={"projects": {}},
                           headers={"Authorization": "Bearer anything"}).status_code == 401
