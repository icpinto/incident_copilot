"""Keep native parser failures outside the API/Temporal worker process."""

from __future__ import annotations

import faulthandler
import logging
import multiprocessing
import os
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import TYPE_CHECKING, Self

from incident_copilot.schemas.index import SymbolRecord

if TYPE_CHECKING:
    from multiprocessing.connection import _ConnectionBase

logger = logging.getLogger(__name__)
_STARTUP_TIMEOUT_SECONDS = 30


def _parse_worker(connection: _ConnectionBase) -> None:
    # Spawn a fresh interpreter: forking a multithreaded Temporal worker is unsafe.
    faulthandler.enable()
    from .treesitter_indexer import TreeSitterFacade

    try:
        parser = TreeSitterFacade()
        connection.send(parser.available)
        if not parser.available:
            return
        while True:
            path, source, grammar = connection.recv()
            try:
                symbols = parser.extract_symbols(Path(path), source, grammar)
                connection.send(
                    [symbol.model_dump() for symbol in symbols] if symbols is not None else None
                )
            except Exception:
                logger.exception("Tree-sitter extraction failed for %s (grammar=%s)", path, grammar)
                connection.send(None)
    except (EOFError, OSError):
        pass  # The indexing run finished or its parent closed the pipe.
    finally:
        connection.close()


class IsolatedTreeSitterFacade:
    """Reuse parsers in a child, disabling a failed grammar for the current run."""

    def __init__(self, timeout_seconds: float = 30) -> None:
        self._timeout = timeout_seconds
        self._context = multiprocessing.get_context("spawn")
        self._process: BaseProcess | None = None
        self._connection: _ConnectionBase | None = None
        self._disabled_grammars: set[str] = set()
        self._available = os.getenv("IC_TREE_SITTER_ENABLED", "true").lower() not in {
            "0", "false", "no", "off",
        }
        if self._available:
            self._start()

    @property
    def available(self) -> bool:
        return self._available

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _start(self) -> None:
        parent, child = self._context.Pipe()
        self._connection = parent
        self._process = self._context.Process(target=_parse_worker, args=(child,), daemon=True)
        try:
            self._process.start()
            child.close()
            if not parent.poll(_STARTUP_TIMEOUT_SECONDS):
                raise TimeoutError("parser startup timed out")
            self._available = parent.recv()
            if not self._available:
                self.close()
        except (EOFError, OSError, RuntimeError, TimeoutError) as exc:
            if self._process.pid is not None:
                self._process.join(timeout=0.1)
            logger.warning(
                "Tree-sitter subprocess unavailable (exitcode=%s, %s: %s); using fallback",
                self._process.exitcode, type(exc).__name__, exc,
            )
            self._available = False
            self.close()
        finally:
            child.close()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        process, self._process = self._process, None
        if process is not None and process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
            if not process.is_alive():
                process.close()

    def extract_symbols(
        self, path: Path, source: str, language_name: str,
    ) -> list[SymbolRecord] | None:
        if not self._available or language_name in self._disabled_grammars:
            return None
        if self._process is None:
            self._start()
        if not self._available:
            return None
        assert self._connection is not None and self._process is not None
        try:
            self._connection.send((str(path), source, language_name))
            if not self._connection.poll(self._timeout):
                raise TimeoutError(f"parsing exceeded {self._timeout}s")
            result = self._connection.recv()
            if result is None:
                self._disabled_grammars.add(language_name)
                logger.warning(
                    "Tree-sitter unavailable for %s (grammar=%s); using fallback", path, language_name,
                )
                return None
            return [SymbolRecord.model_validate(symbol) for symbol in result]
        except (EOFError, OSError, TimeoutError) as exc:
            self._process.join(timeout=0.1)
            logger.warning(
                "Tree-sitter subprocess failed for %s (grammar=%s, exitcode=%s, %s); "
                "disabling this grammar for the indexing run and using fallback",
                path, language_name, self._process.exitcode, type(exc).__name__ + ": " + str(exc),
            )
            self._disabled_grammars.add(language_name)
            self.close()
            return None
