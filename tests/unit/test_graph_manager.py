import asyncio

import httpx
import pytest

from incident_copilot.analysis import graph_manager
from incident_copilot.core.settings import Settings


def config(**values):
    return Settings(_env_file=None, code_graph_mode="managed", gitnexus_url="http://graph.test/mcp", **values)


REAL_CLIENT = httpx.AsyncClient


def transport(monkeypatch, handle):
    client = REAL_CLIENT
    monkeypatch.setattr(graph_manager.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))


def test_defaults_and_token_file_precedence(tmp_path, monkeypatch):
    settings = config(gitnexus_auth_token="old-configured-token")
    assert settings.code_graph_enabled and settings.code_graph_prepare_timeout_seconds == 300
    token = tmp_path / "token"
    token.write_text("generated-token")
    monkeypatch.setenv("IC_GITNEXUS_AUTH_TOKEN_FILE", str(token))
    assert graph_manager.auth_headers(settings) == {"Authorization": "Bearer generated-token"}
    settings.code_graph_mode = "external"
    assert graph_manager.auth_headers(settings) == {"Authorization": "Bearer old-configured-token"}


@pytest.mark.asyncio
async def test_ensure_polls_and_reports_progress_without_exposing_credentials(monkeypatch):
    requests, updates = [], []
    def handle(request):
        requests.append(request)
        return httpx.Response(202 if request.method == "POST" else 200, json={
            "repository": "/repositories/one", "status": "queued" if request.method == "POST" else "ready",
            "indexed_revision": "a" * 40,
        })
    transport(monkeypatch, handle)
    async def progress(state):
        updates.append(state.status)
    ready = await graph_manager.ensure_graph_ready("/repositories/one", "a" * 40, config(), progress)
    assert ready.status == "ready" and updates == ["queued", "ready"]
    assert requests[0].url.path == "/manage/ensure"
    assert requests[1].url.params["repository"] == "/repositories/one"


@pytest.mark.asyncio
async def test_deadline_and_service_failures_are_bounded(monkeypatch):
    transport(monkeypatch, lambda _: httpx.Response(202, json={"status": "preparing"}))
    result = await graph_manager.ensure_graph_ready("/repositories", "a" * 40, config(code_graph_prepare_timeout_seconds=.01))
    assert result.reason == "preparation_timeout"
    transport(monkeypatch, lambda _: httpx.Response(500, text="private-token provider traceback"))
    result = await graph_manager.repository_graph_status("/repositories", config())
    assert result.reason == "manager_unavailable"
    assert "private-token" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"reason": "checkout_not_clean_or_unavailable"}, {"reason": "private-provider-token"}])
async def test_only_fixed_service_reasons_are_exposed(monkeypatch, payload):
    transport(monkeypatch, lambda _: httpx.Response(422, json=payload))
    result = await graph_manager.repository_graph_status("/repositories", config(), refresh=True)
    assert result.reason in {"checkout_not_clean_or_unavailable", "preparation_failed"}


@pytest.mark.asyncio
async def test_disabled_and_external_modes_never_contact_manager(monkeypatch):
    def unexpected(_):
        pytest.fail("external and disabled modes must not contact management endpoints")
    transport(monkeypatch, unexpected)
    assert (await graph_manager.repository_graph_status("repo", config(code_graph_enabled=False))).status == "disabled"
    external = config()
    external.code_graph_mode = "external"
    assert (await graph_manager.repository_graph_status("repo", external, refresh=True)).status == "external"


@pytest.mark.asyncio
async def test_cancelled_preparation_propagates(monkeypatch):
    transport(monkeypatch, lambda _: httpx.Response(202, json={"status": "preparing"}))
    task = asyncio.create_task(graph_manager.ensure_graph_ready("/repositories", "a" * 40, config()))
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

