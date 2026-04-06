import subprocess
from pathlib import Path


def current_branch(repo_root: Path) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "branch", "--show-current"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None
