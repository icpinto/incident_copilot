from pathlib import Path
from typing import Any, Dict

from .ast_python import analyze_python_file
from .ast_typescript import analyze_typescript_file


def deep_analyze_candidate(path: Path) -> Dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return analyze_python_file(path)
    if suffix in {".ts", ".tsx", ".js", ".jsx"}:
        return analyze_typescript_file(path)
    return {"note": "no native deep analysis implemented for this language"}
