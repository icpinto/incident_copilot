from pathlib import Path


def read_text(path: Path, encoding: str = "utf-8") -> str:
    return path.read_text(encoding=encoding, errors="ignore")


def read_snippet(path: Path, line_start: int | None = None, line_end: int | None = None) -> str:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    if line_start is None or line_end is None:
        return "\n".join(lines[:80])
    start = max(line_start - 1, 0)
    end = min(line_end, len(lines))
    return "\n".join(lines[start:end])
