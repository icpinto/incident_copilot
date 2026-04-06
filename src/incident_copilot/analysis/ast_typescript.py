from pathlib import Path
from typing import Any, Dict
import re


def analyze_typescript_file(path: Path) -> Dict[str, Any]:
    source = path.read_text(encoding="utf-8", errors="ignore")
    exports = re.findall(r'export\s+(?:async\s+)?function\s+([A-Za-z0-9_]+)', source)
    classes = re.findall(r'class\s+([A-Za-z0-9_]+)', source)
    imports = re.findall(r'from\s+[\'"]([^\'"]+)[\'"]', source)
    return {
        "functions": sorted(set(exports)),
        "classes": sorted(set(classes)),
        "imports": sorted(set(imports)),
    }
