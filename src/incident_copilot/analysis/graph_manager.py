"""Bounded client for the managed sidecar. Provider text never crosses this boundary."""
from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from incident_copilot.core.settings import Settings
from incident_copilot.schemas.code_graph import GraphPreparation

Progress = Callable[[GraphPreparation], Awaitable[None]]
REASONS = {
    "invalid_repository", "repository_unavailable", "repository_outside_mount",
    "checkout_not_clean_or_unavailable", "source_snapshot_mismatch", "source_changed",
    "manager_stopping", "preparation_failed", "preparation_timeout",
}


def auth_headers(config: Settings) -> dict[str, str]:
    token = config.gitnexus_auth_token
    if config.code_graph_mode == "managed":
        path = Path(os.environ.get("IC_GITNEXUS_AUTH_TOKEN_FILE", "/run/codegraph-auth/token"))
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            # Explicit tokens also support clients outside the Compose network.
            pass
    return {"Authorization": f"Bearer {token}"} if token else {}


def manager_url(config: Settings, path: str) -> str:
    endpoint = urlsplit(config.gitnexus_url)
    return urlunsplit((endpoint.scheme, endpoint.netloc, "/manage/" + path, "", ""))


async def request_status(client: httpx.AsyncClient, config: Settings, repository: str,
                         *, prepare: bool = False, revision: str | None = None, force: bool = False) -> GraphPreparation:
    try:
        if prepare:
            response = await client.post(manager_url(config, "ensure"), json={
                "repository": repository, "revision": revision, "force": force,
            })
        else:
            response = await client.get(manager_url(config, "repositories"), params={"repository": repository})
        if response.status_code == 422:
            reason = response.json().get("reason")
            return GraphPreparation(repository=repository, status="unavailable",
                                    reason=reason if reason in REASONS else "preparation_failed")
        response.raise_for_status()
        result = GraphPreparation.model_validate(response.json())
        # Do not surface arbitrary provider messages even from a malformed service.
        if result.reason is not None and result.reason not in REASONS:
            result.reason = "preparation_failed"
        return result
    except (httpx.HTTPError, ValueError, TypeError):
        return GraphPreparation(repository=repository, status="unavailable", reason="manager_unavailable")


async def repository_graph_status(repository: str, config: Settings, *, refresh: bool = False) -> GraphPreparation:
    if not config.code_graph_enabled:
        return GraphPreparation(repository=repository, status="disabled")
    if config.code_graph_mode == "external":
        return GraphPreparation(repository=repository, status="external")
    async with httpx.AsyncClient(headers=auth_headers(config), timeout=5, trust_env=False) as client:
        return await request_status(client, config, repository, prepare=refresh, force=refresh)


async def ensure_graph_ready(repository: str, revision: str, config: Settings,
                             progress: Progress | None = None) -> GraphPreparation:
    try:
        async with asyncio.timeout(config.code_graph_prepare_timeout_seconds):
            async with httpx.AsyncClient(headers=auth_headers(config), timeout=10, trust_env=False) as client:
                state = await request_status(client, config, repository, prepare=True, revision=revision)
                while True:
                    if progress:
                        await progress(state)
                    if state.status not in {"queued", "preparing"}:
                        return state
                    await asyncio.sleep(1)
                    state = await request_status(client, config, repository)
    except TimeoutError:
        state = GraphPreparation(repository=repository, status="unavailable", reason="preparation_timeout")
        if progress:
            await progress(state)
        return state
