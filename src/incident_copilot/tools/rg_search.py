import shutil
import subprocess
from pathlib import Path
from typing import List


def rg_search(root: Path, pattern: str, max_results: int = 50) -> List[str]:
    if shutil.which("rg") is None:
        return []
    cmd = ["rg", "-n", "--hidden", "--glob", "!.git", pattern, str(root)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode not in (0, 1):
        return []
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    return lines[:max_results]
