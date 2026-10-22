from typing import Dict, List, Optional
from pydantic import BaseModel


class ServiceMapping(BaseModel):
    service_name: str
    build_context: Optional[str] = None
    dockerfile: Optional[str] = None
    env_files: List[str] = []
    command: Optional[str | List[str]] = None
    image: Optional[str] = None


class SymbolRecord(BaseModel):
    name: str
    kind: str
    line_start: int
    line_end: int
    file_path: str


class FileRecord(BaseModel):
    file_path: str
    language: Optional[str] = None
    imports: List[str] = []
    symbols: List[SymbolRecord] = []
    env_refs: List[str] = []
    routes: List[str] = []
    service_hints: List[str] = []
    size_bytes: int = 0
    mtime: float = 0.0


class RepoIndexSummary(BaseModel):
    repo_root: str
    service_mappings: List[ServiceMapping]
    indexed_files: int
    indexed_symbols: int
    db_path: str
