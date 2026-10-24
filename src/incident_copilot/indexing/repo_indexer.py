import logging
import os
from collections.abc import Iterator
from pathlib import Path

from incident_copilot.core.constants import SUPPORTED_CODE_EXTENSIONS
from incident_copilot.schemas.index import FileRecord, RepoIndexSummary
from incident_copilot.tools.fs_utils import safe_relative
from incident_copilot.tools.git_local import clean_revision

from .isolated_parser import IsolatedTreeSitterFacade
from .service_mapper import build_service_map
from .storage import IndexStorage
from .symbol_extractor import (
    extract_env_refs,
    extract_imports,
    extract_lightweight_symbols,
    extract_python_symbols,
    extract_routes,
)

logger = logging.getLogger(__name__)


def _iter_repo_files(repo_root: Path) -> Iterator[Path]:
    ignored = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".cache", ".pytest_cache", ".mypy_cache", ".ruff_cache", "coverage", ".next", ".tox"}
    for directory, subdirectories, filenames in os.walk(repo_root):
        subdirectories[:] = [name for name in subdirectories if name not in ignored and not name.endswith(".egg-info")]
        for filename in filenames:
            path = Path(directory) / filename
            if path.suffix.lower() in SUPPORTED_CODE_EXTENSIONS and not path.is_symlink():
                yield path


def _language_for(path: Path) -> str | None:
    return {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".go": "go",
        ".java": "java",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".json": "json",
    }.get(path.suffix.lower())


def index_repository(repo_root: Path, compose_path: Path | None, db_path: Path) -> RepoIndexSummary:
    revision = clean_revision(repo_root)
    storage = IndexStorage(db_path)
    services = build_service_map(repo_root, compose_path)

    file_records: list[FileRecord] = []
    with IsolatedTreeSitterFacade() as tree_sitter:
        logger.info("Indexing repository %s; tree_sitter_available=%s", repo_root, tree_sitter.available)
        for path in _iter_repo_files(repo_root):
            try:
                source = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                logger.warning("Skipping unreadable source file: %s", path)
                continue

            rel_path = safe_relative(path, repo_root)
            lang = _language_for(path)

            grammar = "tsx" if path.suffix.lower() == ".tsx" else lang
            symbols = tree_sitter.extract_symbols(path, source, grammar) if grammar else None
            if symbols is None:
                if lang == "python":
                    symbols = extract_python_symbols(path, source)
                else:
                    symbols = extract_lightweight_symbols(path, source)

            imports = extract_imports(source)
            env_refs = extract_env_refs(source)
            routes = extract_routes(source)
            service_hints = [
                s.service_name
                for s in services
                if s.build_context and (s.build_context == "." or rel_path.startswith(s.build_context.rstrip("/") + "/"))
            ]

            file_records.append(
                FileRecord(
                    file_path=rel_path,
                    language=lang,
                    imports=imports,
                    symbols=symbols,
                    env_refs=env_refs,
                    routes=routes,
                    service_hints=service_hints,
                    size_bytes=path.stat().st_size,
                    mtime=path.stat().st_mtime,
                )
            )
            if len(file_records) % 100 == 0:
                logger.info("Indexed %s files; latest=%s", len(file_records), rel_path)
    if revision and clean_revision(repo_root) != revision:
        revision = None
    storage.replace_snapshot(repo_root, services, file_records, revision)
    summary = storage.summary()
    logger.info("Repository indexing completed: files=%s symbols=%s", summary.indexed_files, summary.indexed_symbols)
    return summary
