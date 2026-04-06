import logging
from pathlib import Path
from typing import Iterator, List

from incident_copilot.core.constants import SUPPORTED_CODE_EXTENSIONS
from incident_copilot.schemas.index import FileRecord, RepoIndexSummary
from incident_copilot.tools.fs_utils import safe_relative

from .service_mapper import build_service_map
from .storage import IndexStorage
from .symbol_extractor import (
    extract_env_refs,
    extract_imports,
    extract_lightweight_symbols,
    extract_python_symbols,
    extract_routes,
)
from .treesitter_indexer import TreeSitterFacade

logger = logging.getLogger(__name__)


def _iter_repo_files(repo_root: Path) -> Iterator[Path]:
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        if ".git" in path.parts or "node_modules" in path.parts or "__pycache__" in path.parts:
            continue
        if path.suffix.lower() in SUPPORTED_CODE_EXTENSIONS:
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
    storage = IndexStorage(db_path)
    storage.save_repo_root(repo_root)
    services = build_service_map(repo_root, compose_path)
    storage.save_services(services)

    tree_sitter = TreeSitterFacade()
    logger.info("tree_sitter_available=%s", tree_sitter.available)

    file_records: List[FileRecord] = []
    for path in _iter_repo_files(repo_root):
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        rel_path = safe_relative(path, repo_root)
        lang = _language_for(path)

        if path.suffix.lower() == ".py":
            symbols = extract_python_symbols(path, source)
        else:
            symbols = extract_lightweight_symbols(path, source)

        imports = extract_imports(source)
        env_refs = extract_env_refs(source)
        routes = extract_routes(source)
        service_hints = [
            s.service_name
            for s in services
            if s.build_context and rel_path.startswith(s.build_context.rstrip("/") + "/")
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
    storage.save_files(file_records)
    return storage.summary()
