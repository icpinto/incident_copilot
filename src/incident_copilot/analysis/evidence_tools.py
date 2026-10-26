"""Bounded retrieval against a trusted, clean source/index snapshot.

Model requests select evidence, never repository roots, provider settings, or commands.
All operations are read-only and every returned location belongs to the local index.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from incident_copilot.analysis.code_graph import (
    MAX_RELATED_SYMBOLS,
    GraphEvidenceError,
    relative_path,
    retrieve_symbol_context,
)
from incident_copilot.core.settings import Settings
from incident_copilot.indexing.storage import IndexStorage
from incident_copilot.llm.validation import schema_diagnostic, validation_failure
from incident_copilot.schemas.index import FileRecord
from incident_copilot.schemas.investigation import (
    EvidenceItem,
    EvidenceRequest,
    SymbolReference,
    ToolOutcome,
)
from incident_copilot.tools.git_local import clean_revision

MAX_SOURCE_LINES = 80
MAX_TOOL_BYTES = 8192
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b[\w.-]*(?:password|passwd|secret|api[_-]?key|auth[_-]?token|access[_-]?token|"
    r"private[_-]?key|client[_-]?secret)[\w.-]*[\"']?\s*[:=]\s*).*$"
)
_CREDENTIAL_URL = re.compile(r"(https?://)[^\s/:@]+:[^\s/@]+@", re.IGNORECASE)
_PRIVATE_KEY = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.DOTALL)


def _encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _clip(value: str, max_bytes: int) -> str:
    return value.encode("utf-8")[:max(0, max_bytes)].decode("utf-8", errors="ignore")


def _symbol_key(symbol: dict[str, Any]) -> str:
    return json.dumps([symbol.get(k) for k in ("file_path", "name", "line_start", "line_end")], separators=(",", ":"))


def _evidence_id(item: EvidenceItem) -> str:
    provenance = item.model_dump(exclude={"id", "request_id"})
    if item.kind == "code_graph":
        # Provider request timing is operational metadata, not new source evidence.
        provenance["data"].pop("retrieval_ms", None)
    return "ev_" + hashlib.sha256(_encoded(provenance)).hexdigest()[:24]


def _configured_secrets(settings: Settings) -> list[str]:
    return [str(value) for name, value in settings.model_dump().items()
            if any(word in name.lower() for word in ("password", "secret", "api_key", "auth_token"))
            and value and str(value) != "dummy"]


def _redact_text(text: str, secrets: list[str]) -> str:
    # Preserve newlines so source evidence still cites the original physical lines.
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]" + "\n" * secret.count("\n"))
    text = _CREDENTIAL_URL.sub(r"\1[REDACTED]@", text)
    text = _PRIVATE_KEY.sub(lambda match: "[REDACTED PRIVATE KEY]" + "\n" * match[0].count("\n"), text)
    return "\n".join(_SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", line) for line in text.split("\n"))


def _redact_value(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if isinstance(value, list):
        return [_redact_value(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item, secrets) for key, item in value.items()}
    return value


def redact_evidence(value: Any, settings: Settings) -> Any:
    """Scrub configured credentials and recognizable source secrets without moving lines."""
    return _redact_value(value, _configured_secrets(settings))


class EvidenceTools:
    def __init__(self, repo_root: Path, index_db_path: Path, settings: Settings, snapshot: dict[str, Any]):
        self.root = Path(repo_root).resolve()
        self.index_db_path = Path(index_db_path)
        self.settings = settings
        self.snapshot = snapshot
        self._graph_symbols: set[str] = set()
        try:
            self.storage = IndexStorage(self.index_db_path, read_only=True)
        except (OSError, sqlite3.Error):
            raise ValueError("index_unavailable") from None
        self._secrets = _configured_secrets(settings)

    @property
    def graph_symbol_ids(self) -> list[str]:
        return sorted(self._graph_symbols)

    def restore_graph_symbols(self, symbols: list[dict[str, Any]] | list[str]) -> None:
        """Restore consumed graph capacity from persisted evidence before resuming."""
        for symbol in symbols:
            self._graph_symbols.add(_symbol_key(symbol) if isinstance(symbol, dict) else symbol)

    def validate_source(self) -> None:
        revision = self.snapshot.get("revision")
        if not revision or clean_revision(self.root) != revision:
            raise ValueError("source_changed")
        try:
            indexed_root, indexed_revision = self.storage.snapshot_identity()
        except (OSError, sqlite3.Error):
            raise ValueError("index_unavailable") from None
        expected_root = self.snapshot.get("index_root") or str(self.root)
        expected_revision = self.snapshot.get("index_revision") or revision
        if (not indexed_root or Path(indexed_root).resolve() != self.root
                or Path(expected_root).resolve() != self.root
                or indexed_revision != revision or indexed_revision != expected_revision):
            raise ValueError("source_snapshot_mismatch")

    def _file(self, file_path: str) -> tuple[FileRecord, Path]:
        try:
            file_path = relative_path(file_path)
        except GraphEvidenceError:
            raise ValueError("invalid_source_location") from None
        path = (self.root / file_path).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("invalid_source_location")
        record = self.storage.get_file_record(file_path)
        if record is None:
            raise ValueError("unindexed_source_location")
        return record, path

    def _symbol(self, ref: SymbolReference) -> SymbolReference:
        record, _ = self._file(ref.file_path)
        matches = [symbol for symbol in record.symbols if (
            symbol.name, symbol.line_start, symbol.line_end
        ) == (ref.name, ref.line_start, ref.line_end)]
        if len(matches) != 1:
            raise ValueError("unverified_symbol")
        return ref

    def _redact(self, text: str) -> str:
        return _redact_text(text, self._secrets)

    def _redact_data(self, value: Any) -> Any:
        return _redact_value(value, self._secrets)

    def _item(self, kind: str, request_id: str, *, content: str = "", data: dict[str, Any] | None = None,
              file_path: str | None = None, line_start: int | None = None,
              line_end: int | None = None, limitations: list[str] | None = None,
              truncated: bool = False) -> EvidenceItem:
        item = EvidenceItem(
            id="", kind=kind, repository=self.snapshot["repository"], revision=self.snapshot["revision"],
            file_path=file_path, line_start=line_start, line_end=line_end, content=self._redact(content),
            data=self._redact_data(data or {}), limitations=limitations or [], request_id=request_id, truncated=truncated,
        )
        # Requests and elapsed time do not alter an evidence identity.
        item.id = _evidence_id(item)
        return item

    def _records(self, request: EvidenceRequest) -> list[FileRecord]:
        if request.file_path:
            records = [self._file(request.file_path)[0]]
        else:
            records = self.storage.get_file_records()
        return sorted((record for record in records
                       if (not request.language or record.language == request.language)
                       and (not request.service or request.service in record.service_hints)),
                      key=lambda record: record.file_path)

    def _find_symbols(self, request: EvidenceRequest, request_id: str) -> list[EvidenceItem]:
        matches = []
        for record in self._records(request):
            self._file(record.file_path)
            for symbol in record.symbols:
                if request.query.casefold() in symbol.name.casefold():
                    matches.append(SymbolReference(
                        file_path=record.file_path, name=symbol.name,
                        line_start=symbol.line_start, line_end=symbol.line_end,
                    ).model_dump())
        total = len(matches)
        matches = matches[:request.limit]
        if not matches:
            return []
        limitations = ["symbol_match_is_navigation_not_runtime_evidence"]
        if total > len(matches):
            limitations.append("result_limit")
        return [self._item("symbols", request_id, data={"symbols": matches, "total_matches": total,
                          "ambiguous": total > 1}, limitations=limitations, truncated=total > len(matches))]

    def _read_source(self, request: EvidenceRequest, request_id: str) -> list[EvidenceItem]:
        symbol = self._symbol(request.symbol) if request.symbol else None
        file_path = symbol.file_path if symbol else request.file_path
        _, path = self._file(file_path)
        start = request.line_start or (symbol.line_start if symbol else 1)
        end = request.line_end or (symbol.line_end if symbol else start + MAX_SOURCE_LINES - 1)
        # Redact before slicing: an interior range must not expose a PEM key body.
        lines = self._redact(path.read_text(encoding="utf-8", errors="replace")).splitlines()
        if start > len(lines):
            return []
        requested_end = end
        end = min(end, start + MAX_SOURCE_LINES - 1, len(lines))
        raw = "\n".join(lines[start - 1:end])
        redacted = self._redact(raw)
        content = _clip(redacted, 6500)
        truncated = end < min(requested_end, len(lines)) or content != redacted
        if content != redacted:
            # Exclude an incomplete final line; the next read can start at the following line.
            content = content.rsplit("\n", 1)[0] if "\n" in content else content
            end = start + content.count("\n")
        return [self._item(
            "source", request_id, file_path=file_path, line_start=start, line_end=end,
            content=content, data={"symbol": symbol.model_dump() if symbol else None,
                                   "next_line": end + 1 if truncated else None},
            limitations=["source_is_static_evidence"] + (["response_clipped"] if truncated else []),
            truncated=truncated,
        )]

    def _search_source(self, request: EvidenceRequest, request_id: str) -> list[EvidenceItem]:
        matches: list[EvidenceItem] = []
        # Search only indexed content and redact before matching or publishing.
        for record in self._records(request):
            _, path = self._file(record.file_path)
            for line_number, line in enumerate(self._redact(path.read_text(encoding="utf-8", errors="replace")).splitlines(), 1):
                if request.query in line:
                    matches.append(self._item(
                        "search_match", request_id, file_path=record.file_path,
                        line_start=line_number, line_end=line_number, content=_clip(line, 1024),
                        limitations=["literal_search_match"], truncated=len(line.encode("utf-8")) > 1024,
                    ))
                    if len(matches) >= request.limit:
                        return matches
        return matches

    def _get_file_context(self, request: EvidenceRequest, request_id: str) -> list[EvidenceItem]:
        record, _ = self._file(request.file_path)
        data = record.model_dump(include={"language", "imports", "env_refs", "routes", "service_hints", "symbols"})
        # The indexer's nested symbol paths may be absolute. Every exposed
        # navigation reference must use the same verified repository-relative path.
        for symbol in data["symbols"]:
            symbol["file_path"] = record.file_path
        # Keep names and navigation metadata, never service command/environment values.
        return [self._item("file_context", request_id, file_path=record.file_path, data=data,
                           limitations=["index_contains_navigation_metadata_not_data_flow"])]

    async def _get_related_calls(self, request: EvidenceRequest, request_id: str) -> tuple[list[EvidenceItem], dict[str, Any] | None, str | None]:
        symbol = self._symbol(request.symbol)
        generation = self.snapshot.get("graph_generation")
        if isinstance(generation, dict):
            if generation.get("lastCommit") != self.snapshot["revision"]:
                raise ValueError("source_snapshot_mismatch")
            generation = generation.get("indexedAt")
        context = await retrieve_symbol_context(
            self.root, self.index_db_path, symbol.model_dump(), self.settings,
            expected_generation=generation,
        )
        if context is None:
            return [], None, "code_graph_disabled"
        if context.get("status") not in {"available", "partial"}:
            reason = context.get("reason") or "no_usable_graph_context"
            if reason == "stale_index" and self.snapshot.get("graph_generation"):
                raise ValueError("source_changed")
            return [], context, reason
        if context.get("indexed_at"):
            self.snapshot["graph_generation"] = {
                "lastCommit": context.get("indexed_revision") or self.snapshot["revision"],
                "indexedAt": context["indexed_at"],
            }
        anchor = next((item for item in context["symbols"] if _symbol_key(item) == _symbol_key(symbol.model_dump())), None)
        if anchor is None:
            raise ValueError("unverified_symbol")
        edges = [edge for edge in context["relationships"]
                 if request.direction == "both"
                 or (request.direction == "incoming" and edge["target_uid"] == anchor["uid"])
                 or (request.direction == "outgoing" and edge["source_uid"] == anchor["uid"])]
        wanted = {uid for edge in edges for uid in (edge["source_uid"], edge["target_uid"])}
        related = [item for item in context["symbols"] if item["uid"] != anchor["uid"] and item["uid"] in wanted]
        accepted = []
        clipped = False
        for item in related:
            if len(accepted) >= request.limit or (_symbol_key(item) not in self._graph_symbols
                                                and len(self._graph_symbols) + sum(_symbol_key(s) not in self._graph_symbols for s in accepted) >= MAX_RELATED_SYMBOLS):
                clipped = True
                continue
            accepted.append(item)
        # Provider details occur both in a citable evidence item and report graph context.
        # Leave room for the outcome envelope and provenance within the 8 KiB response cap.
        def select():
            context["symbols"] = [anchor] + accepted
            included = {item["uid"] for item in context["symbols"]}
            context["relationships"] = [edge for edge in edges if edge["source_uid"] in included and edge["target_uid"] in included]
        select()
        while len(_encoded(context)) > 3100 and accepted:
            accepted.pop()
            clipped = True
            select()
        if clipped:
            context["status"], context["completeness"] = "partial", "lower_bound"
            context["limitations"] = list(dict.fromkeys(context["limitations"] + ["investigation_related_symbol_or_response_limit"]))
        self._graph_symbols.update(_symbol_key(item) for item in accepted)
        return [self._item(
            "code_graph", request_id, file_path=symbol.file_path,
            line_start=symbol.line_start, line_end=symbol.line_end, data=context,
            limitations=context["limitations"], truncated=clipped,
        )], context, None

    def _bound(self, outcome: ToolOutcome) -> ToolOutcome:
        # Include clipping metadata before measuring the final response envelope.
        if len(_encoded(outcome.model_dump(mode="json"))) > MAX_TOOL_BYTES:
            outcome.reason = "response_clipped"
        while len(_encoded(outcome.model_dump(mode="json"))) > MAX_TOOL_BYTES and outcome.evidence:
            last = outcome.evidence[-1]
            if last.content and len(last.content.encode("utf-8")) > 256:
                last.content = _clip(last.content, max(256, len(last.content.encode("utf-8")) // 2))
                if last.kind == "source" and last.line_start is not None:
                    if "\n" in last.content:
                        last.content = last.content.rsplit("\n", 1)[0]
                    last.line_end = last.line_start + last.content.count("\n")
                    last.data["next_line"] = last.line_end + 1
                last.truncated = True
                last.limitations = list(dict.fromkeys(last.limitations + ["response_clipped"]))
                last.id = _evidence_id(last)
            elif last.kind != "code_graph" and any(isinstance(value, list) and value for value in last.data.values()):
                key = max((key for key, value in last.data.items() if isinstance(value, list) and value),
                          key=lambda key: len(_encoded(last.data[key])))
                last.data[key].pop()
                last.truncated = True
                last.limitations = list(dict.fromkeys(last.limitations + ["response_clipped"]))
                last.id = _evidence_id(last)
            else:
                outcome.evidence.pop()
        if len(_encoded(outcome.model_dump(mode="json"))) > MAX_TOOL_BYTES:
            outcome.graph_context = None
            outcome.reason = "response_clipped"
        while (outcome.validation_error and len(outcome.validation_error.issues) > 1
               and len(_encoded(outcome.model_dump(mode="json"))) > MAX_TOOL_BYTES):
            outcome.validation_error.issues.pop()
        if not outcome.evidence and outcome.status == "ok":
            outcome.status = "empty"
        return outcome

    async def execute(self, request: EvidenceRequest, request_id: str) -> ToolOutcome:
        started = time.monotonic()
        try:
            await asyncio.to_thread(self.validate_source)
            graph_context = None
            reason = None
            if request.tool == "get_related_calls":
                evidence, graph_context, reason = await self._get_related_calls(request, request_id)
            else:
                operation = getattr(self, "_" + request.tool)
                evidence = await asyncio.to_thread(operation, request, request_id)
            await asyncio.to_thread(self.validate_source)
            outcome = ToolOutcome(
                request_id=request_id, tool=request.tool,
                status="unavailable" if reason else "ok" if evidence else "empty",
                reason=reason, evidence=evidence, graph_context=graph_context,
            )
        except ValidationError as exc:
            outcome = ToolOutcome(request_id=request_id, tool=request.tool, status="invalid",
                reason="evidence_validation_failed", validation_error=schema_diagnostic(
                    exc, ToolOutcome, stage="evidence_retrieval", request_id=request_id))
        except ValueError as exc:
            allowed = {"source_changed", "source_snapshot_mismatch", "index_unavailable", "invalid_source_location",
                       "unindexed_source_location", "unverified_symbol"}
            outcome = ToolOutcome(request_id=request_id, tool=request.tool, status="invalid",
                                  reason=str(exc) if str(exc) in allowed else "invalid_evidence_request")
            if outcome.reason is not None and outcome.reason in {
                "invalid_source_location", "unindexed_source_location", "unverified_symbol", "invalid_evidence_request",
            }:
                field = "symbol" if request.symbol else "file_path"
                outcome.validation_error = validation_failure(stage="evidence_retrieval", kind="semantic",
                    code=outcome.reason, path=[field], request_id=request_id).diagnostic
        except (OSError, sqlite3.Error):
            outcome = ToolOutcome(request_id=request_id, tool=request.tool, status="unavailable", reason="source_unavailable")
        outcome.duration_ms = round((time.monotonic() - started) * 1000, 2)
        return self._bound(outcome)
