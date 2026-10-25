"""Optional GitNexus retrieval. No ranking changes, source execution, or graph writes."""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from incident_copilot.core.settings import Settings
from incident_copilot.indexing.storage import IndexStorage
from incident_copilot.schemas.code_graph import (
    GraphContext,
    GraphRelationship,
    GraphSeedResult,
    GraphSymbol,
)
from incident_copilot.tools.git_local import clean_revision

GITNEXUS_VERSION = "1.6.12"
MAX_SEEDS = 3
MAX_RELATED_SYMBOLS = 20
MAX_RESPONSE_BYTES = 131072


class GraphEvidenceError(Exception):
    """Only fixed reason codes, never provider messages or credentials, cross the boundary."""


class CodeGraphProvider(Protocol):
    version: str

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


def normalize_mcp_result(result: Any) -> dict[str, Any]:
    """GitNexus 1.6.12 emits one JSON value followed by optional Markdown hints."""
    if result.isError:
        raise GraphEvidenceError("provider_error")
    blocks = result.content
    if len(blocks) != 1 or getattr(blocks[0], "type", None) != "text":
        raise GraphEvidenceError("malformed_response")
    text = blocks[0].text.strip()
    if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise GraphEvidenceError("response_too_large")
    # maxTokens may cut the JSON OR the appended hint. Neither is silently accepted.
    if text.endswith("…"):
        raise GraphEvidenceError("truncated_response")
    try:
        payload, end = json.JSONDecoder().raw_decode(text)
    except (ValueError, RecursionError):
        raise GraphEvidenceError("malformed_response") from None
    suffix = text[end:].strip()
    if suffix and not suffix.startswith("---\n**Next:"):
        raise GraphEvidenceError("malformed_response")
    if not isinstance(payload, dict):
        raise GraphEvidenceError("malformed_response")
    if payload.get("error"):
        raise GraphEvidenceError("provider_error")
    if payload.get("truncated") or payload.get("partial"):
        raise GraphEvidenceError("truncated_response")
    return payload


@asynccontextmanager
async def open_provider(config: Settings) -> AsyncIterator[CodeGraphProvider]:
    # The optional extra is unnecessary for the existing deterministic pipeline.
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {config.gitnexus_auth_token}"} if config.gitnexus_auth_token else {}
    async with (
        httpx.AsyncClient(headers=headers, timeout=config.code_graph_timeout_seconds, trust_env=False) as client,
        streamable_http_client(config.gitnexus_url, http_client=client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        initialized = await session.initialize()
        if initialized.serverInfo.name != "gitnexus" or initialized.serverInfo.version != GITNEXUS_VERSION:
            raise GraphEvidenceError("unsupported_provider_version")

        class GitNexusProvider:
            version = initialized.serverInfo.version

            async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                if tool not in {"list_repos", "context"}:
                    raise GraphEvidenceError("unsupported_tool")
                return normalize_mcp_result(await session.call_tool(tool, arguments))

        yield GitNexusProvider()


def relative_path(value: Any) -> str:
    if not isinstance(value, str) or "\\" in value or ":" in value:
        raise GraphEvidenceError("invalid_source_location")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.as_posix() != value:
        raise GraphEvidenceError("invalid_source_location")
    return value


def graph_symbol(raw: Any, storage: IndexStorage, root: Path) -> GraphSymbol:
    if not isinstance(raw, dict):
        raise GraphEvidenceError("malformed_response")
    file_path = relative_path(raw.get("filePath"))
    path = root / file_path
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise GraphEvidenceError("invalid_source_location")
    record = storage.get_file_record(file_path)
    if record is None:
        raise GraphEvidenceError("unindexed_source_location")
    uid, name = raw.get("uid"), raw.get("name")
    if not isinstance(uid, str) or not isinstance(name, str):
        raise GraphEvidenceError("malformed_response")
    try:
        symbol = GraphSymbol(
            uid=uid, name=name, file_path=file_path,
            kind=raw.get("kind", "unknown"), line_start=raw.get("startLine"),
            line_end=raw.get("endLine"),
        )
    except ValidationError:
        raise GraphEvidenceError("malformed_response") from None
    matches = [s for s in record.symbols if s.name == symbol.name]
    if symbol.line_start is not None:
        matches = [s for s in matches if s.line_start == symbol.line_start]
    if symbol.line_end is not None:
        matches = [s for s in matches if s.line_end == symbol.line_end]
    if len(matches) != 1:
        raise GraphEvidenceError("ambiguous_source_location")
    symbol.line_start, symbol.line_end = matches[0].line_start, matches[0].line_end
    return symbol


def check_staleness(payload: dict[str, Any]) -> None:
    stale = payload.get("staleness")
    # Current indexes omit staleness; unknown/behind/diverged must fail closed.
    if stale is not None and (not isinstance(stale, dict) or stale.get("status") != "current"):
        raise GraphEvidenceError("stale_index")


async def retrieve_graph_context(
    root: Path, index_db_path: Path, candidates: list[dict[str, Any]], config: Settings,
    *, expected_generation: str | None = None,
) -> dict[str, Any] | None:
    if not config.code_graph_enabled:
        return None
    started = time.monotonic()
    context = GraphContext(repository=config.gitnexus_repository or str(root.resolve()), status="skipped")
    try:
        async with asyncio.timeout(config.code_graph_timeout_seconds):
            await _retrieve(root, index_db_path, candidates, config, context, expected_generation)
    except TimeoutError:
        context.status, context.reason = "unavailable", "retrieval_timeout"
        context.symbols, context.relationships = [], []
    except Exception as exc:  # noqa: BLE001 -- Optional providers must preserve diagnosis; never persist exception text.
        reason = _reason(exc)
        context.status = "skipped" if reason in {"stale_index", "source_changed"} else "unavailable"
        context.reason = reason
        context.symbols, context.relationships = [], []
    context.retrieval_ms = round((time.monotonic() - started) * 1000, 2)
    return context.model_dump(mode="json")


async def retrieve_symbol_context(
    root: Path, index_db_path: Path, symbol: dict[str, Any], config: Settings,
    *, expected_generation: str | None = None,
) -> dict[str, Any] | None:
    """Retrieve a verified single symbol's neighbors without recursively traversing them."""
    seed = {
        "file_path": symbol["file_path"], "symbol_name": symbol["name"],
        "line_start": symbol["line_start"], "line_end": symbol["line_end"],
    }
    return await retrieve_graph_context(
        root, index_db_path, [seed], config, expected_generation=expected_generation,
    )


def _reason(exc: Exception) -> str:
    if isinstance(exc, GraphEvidenceError):
        return str(exc)
    if isinstance(exc, ExceptionGroup):
        for nested in exc.exceptions:
            reason = _reason(nested)
            if reason != "provider_unavailable":
                return reason
    if isinstance(exc, ImportError):
        return "dependency_unavailable"
    return "provider_unavailable"


async def _retrieve(root: Path, index_db_path: Path, candidates: list[dict[str, Any]], config: Settings, context: GraphContext, expected_generation: str | None = None) -> None:
    context.repo_revision = await asyncio.to_thread(clean_revision, root)
    if context.repo_revision is None:
        context.reason = "checkout_not_clean_or_unavailable"
        return
    storage = IndexStorage(index_db_path, read_only=True)
    indexed_root, indexed_revision = storage.snapshot_identity()
    if Path(indexed_root).resolve() != root.resolve() or indexed_revision != context.repo_revision:
        context.reason = "source_snapshot_mismatch"
        return
    seeds = [c for c in candidates if c.get("symbol_name") and c.get("line_start") and c.get("line_end")][:MAX_SEEDS]
    if not seeds:
        context.reason = "no_localized_symbols"
        return
    async with open_provider(config) as provider:
        context.provider_version = provider.version
        before = await repository_snapshot(provider, context.repository)
        context.indexed_revision = before["lastCommit"]
        context.indexed_at = before.get("indexedAt")
        if expected_generation is not None and context.indexed_at != expected_generation:
            raise GraphEvidenceError("stale_index")
        if context.indexed_revision != context.repo_revision:
            raise GraphEvidenceError("stale_index")
        symbols: dict[str, GraphSymbol] = {}
        edges: dict[tuple[str, str], GraphRelationship] = {}
        related: set[str] = set()
        context.completeness = "bounded"
        context.limitations = ["static_paths_do_not_prove_runtime_execution", "provider_reference_windows_are_bounded"]
        for seed in seeds:
            result = GraphSeedResult(file_path=seed["file_path"], symbol_name=seed["symbol_name"], status="skipped")
            context.seeds.append(result)
            try:
                payload = await provider.call("context", {
                    "repo": context.repository, "name": seed["symbol_name"],
                    "file_path": seed["file_path"], "include_content": False, "maxTokens": 16000,
                })
                check_staleness(payload)
                if payload.get("status") == "ambiguous":
                    # The pinned provider cannot accept a line argument. Resolve only an exact
                    # file/name/start-line match, then fetch that UID and verify its full range.
                    matches = [c for c in payload.get("candidates", []) if isinstance(c, dict)
                               and c.get("filePath") == seed["file_path"] and c.get("name") == seed["symbol_name"]
                               and c.get("line") == seed["line_start"]]
                    if len(matches) != 1 or payload.get("candidatesTruncated"):
                        raise GraphEvidenceError("ambiguous_symbol")
                    payload = await provider.call("context", {
                        "repo": context.repository, "uid": matches[0]["uid"],
                        "include_content": False, "maxTokens": 16000,
                    })
                    check_staleness(payload)
                if payload.get("status") != "found":
                    raise GraphEvidenceError("symbol_not_found")
                anchor = graph_symbol(payload.get("symbol"), storage, root)
                if (anchor.file_path, anchor.name, anchor.line_start, anchor.line_end) != (
                    seed["file_path"], seed["symbol_name"], seed["line_start"], seed["line_end"],
                ):
                    raise GraphEvidenceError("symbol_location_mismatch")
                pending = []
                for direction in ("incoming", "outgoing"):
                    refs = payload.get(direction)
                    if not isinstance(refs, dict) or not isinstance(refs.get("calls", []), list):
                        raise GraphEvidenceError("malformed_response")
                    for raw in refs.get("calls", []):
                        try:
                            pending.append((direction, graph_symbol(raw, storage, root)))
                        except GraphEvidenceError as exc:
                            if str(exc) != "ambiguous_source_location":
                                raise
                            # Pinned context refs omit ranges. An unresolved same-file
                            # method must not hide other independently verified calls.
                            result.reason = "ambiguous_related_symbol"
                            context.completeness = "lower_bound"
                            if "ambiguous_related_symbols_omitted" not in context.limitations:
                                context.limitations.append("ambiguous_related_symbols_omitted")
                symbols[anchor.uid] = anchor
                for direction, neighbor in pending:
                    if neighbor.uid not in related and len(related) >= MAX_RELATED_SYMBOLS:
                        result.reason = "related_symbol_limit"
                        continue
                    related.add(neighbor.uid)
                    symbols[neighbor.uid] = neighbor
                    source, target = (neighbor.uid, anchor.uid) if direction == "incoming" else (anchor.uid, neighbor.uid)
                    edges[(source, target)] = GraphRelationship(source_uid=source, target_uid=target)
                result.status = "found"
                if payload.get("epistemic") != "exact":
                    context.completeness = "lower_bound"
                    result.reason = result.reason or "provider_lower_bound"
            except GraphEvidenceError as exc:
                if str(exc) == "stale_index":
                    raise
                result.reason = str(exc)
        # Recheck the graph generation and checkout after all calls: never mix refreshes.
        after = await repository_snapshot(provider, context.repository)
        if (after["lastCommit"], after.get("indexedAt")) != (before["lastCommit"], before.get("indexedAt")):
            raise GraphEvidenceError("stale_index")
        if await asyncio.to_thread(clean_revision, root) != context.repo_revision:
            raise GraphEvidenceError("source_changed")
        context.symbols, context.relationships = list(symbols.values()), list(edges.values())
        found = any(s.status == "found" for s in context.seeds)
        context.status = "partial" if found and any(s.reason for s in context.seeds) else "available" if found else "skipped"
        context.reason = None if found else "no_usable_graph_context"


async def repository_snapshot(provider: CodeGraphProvider, selection: str) -> dict[str, Any]:
    listing = await provider.call("list_repos", {"limit": 100, "offset": 0})
    repos = listing.get("repositories")
    if not isinstance(repos, list):
        raise GraphEvidenceError("malformed_response")
    matches = [r for r in repos if isinstance(r, dict) and (r.get("path") == selection or r.get("name") == selection)]
    if len(matches) != 1:
        raise GraphEvidenceError("repository_not_found_or_ambiguous")
    snapshot = matches[0]
    check_staleness(snapshot)
    if not isinstance(snapshot.get("lastCommit"), str) or not snapshot["lastCommit"]:
        raise GraphEvidenceError("unverified_index_revision")
    if not isinstance(snapshot.get("indexedAt"), str) or not snapshot["indexedAt"]:
        raise GraphEvidenceError("unverified_index_generation")
    return snapshot
