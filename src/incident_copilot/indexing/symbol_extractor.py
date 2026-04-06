import ast
import re
from pathlib import Path
from typing import List, Optional

from incident_copilot.schemas.index import SymbolRecord

IMPORT_RE = re.compile(r'^\s*(?:from\s+([a-zA-Z0-9_\.]+)\s+import|import\s+([a-zA-Z0-9_\.]+))')
ENV_RE = re.compile(r'(?:os\.getenv|os\.environ\.get|process\.env\.)(?:\(|\[)?["\']?([A-Z0-9_]+)')
ROUTE_RE = re.compile(r'["\'](/[^"\']+)["\']')


def extract_python_symbols(path: Path, source: str) -> List[SymbolRecord]:
    symbols: List[SymbolRecord] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return symbols
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(
                SymbolRecord(
                    name=node.name,
                    kind="function",
                    line_start=node.lineno,
                    line_end=getattr(node, "end_lineno", node.lineno),
                    file_path=str(path),
                )
            )
        elif isinstance(node, ast.ClassDef):
            symbols.append(
                SymbolRecord(
                    name=node.name,
                    kind="class",
                    line_start=node.lineno,
                    line_end=getattr(node, "end_lineno", node.lineno),
                    file_path=str(path),
                )
            )
    return symbols


def extract_lightweight_symbols(path: Path, source: str) -> List[SymbolRecord]:
    symbols: List[SymbolRecord] = []
    for i, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("def "):
            name = stripped.split("def ", 1)[1].split("(", 1)[0].strip()
            symbols.append(SymbolRecord(name=name, kind="function", line_start=i, line_end=i, file_path=str(path)))
        elif stripped.startswith("class "):
            name = stripped.split("class ", 1)[1].split("(", 1)[0].split(":", 1)[0].strip()
            symbols.append(SymbolRecord(name=name, kind="class", line_start=i, line_end=i, file_path=str(path)))
        elif re.match(r"^(export\s+)?(async\s+)?function\s+[A-Za-z0-9_]+", stripped):
            name = stripped.split("function", 1)[1].split("(", 1)[0].strip()
            symbols.append(SymbolRecord(name=name, kind="function", line_start=i, line_end=i, file_path=str(path)))
    return symbols


def extract_imports(source: str) -> List[str]:
    imports: List[str] = []
    for line in source.splitlines():
        m = IMPORT_RE.match(line)
        if not m:
            continue
        imports.append((m.group(1) or m.group(2) or "").strip())
    return imports


def extract_env_refs(source: str) -> List[str]:
    return sorted(set(ENV_RE.findall(source)))


def extract_routes(source: str) -> List[str]:
    routes = [r for r in ROUTE_RE.findall(source) if len(r) < 128]
    return sorted(set(routes))
