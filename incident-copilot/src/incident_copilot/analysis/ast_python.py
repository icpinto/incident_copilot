import ast
from pathlib import Path
from typing import Any, Dict


def analyze_python_file(path: Path) -> Dict[str, Any]:
    source = path.read_text(encoding="utf-8", errors="ignore")
    result: Dict[str, Any] = {
        "functions": [],
        "classes": [],
        "imports": [],
        "raises": [],
    }
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        result["syntax_error"] = str(exc)
        return result

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result["functions"].append(node.name)
        elif isinstance(node, ast.ClassDef):
            result["classes"].append(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                result["imports"].append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            result["imports"].append(node.module or "")
        elif isinstance(node, ast.Raise):
            if isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name):
                result["raises"].append(node.exc.func.id)
            elif isinstance(node.exc, ast.Name):
                result["raises"].append(node.exc.id)
    result["imports"] = sorted(set(result["imports"]))
    result["raises"] = sorted(set(result["raises"]))
    return result
