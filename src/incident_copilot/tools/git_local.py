import subprocess
from pathlib import Path


def clean_revision(repo_root: Path) -> str | None:
    """Return HEAD only for an exact, clean checkout root; never execute repository code."""
    try:
        def git(*args: str) -> str:
            proc = subprocess.run(
                ["git", "--no-optional-locks", "-c", f"safe.directory={repo_root.resolve().as_posix()}", "-C", str(repo_root), *args], capture_output=True, text=True,
                timeout=2, check=True,
            )
            return proc.stdout.strip()

        if Path(git("rev-parse", "--show-toplevel")).resolve() != repo_root.resolve():
            return None
        revision = git("rev-parse", "HEAD")
        if git("status", "--porcelain", "--untracked-files=normal", "--ignore-submodules=none"):
            return None
        # Detect a concurrent checkout during the status probe.
        return revision if revision == git("rev-parse", "HEAD") else None
    except (OSError, subprocess.SubprocessError):
        return None


def current_branch(repo_root: Path) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "branch", "--show-current"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None
