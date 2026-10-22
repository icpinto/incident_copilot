"""Match stack locations across repository, container, and Windows paths."""

import posixpath
from pathlib import Path

from incident_copilot.schemas.index import FileRecord, SymbolRecord
from incident_copilot.schemas.logs import StackFrame


def _normalized_path(path: str) -> str:
    return posixpath.normpath(path.removeprefix("file://").replace("\\", "/")).casefold()


def stack_path_match(
    file_path: str,
    trace_path: str | None,
    repo_root: Path | None = None,
) -> tuple[int, int]:
    """Return match quality and suffix length; full paths outrank basename hints.

    Container mount prefixes need not match the host. Compare whole trailing
    components, so /app/services/api/main.py resolves to services/api/main.py,
    while main.py never matches domain.py or main.py.bak.
    """
    if not trace_path:
        return (0, 0)
    candidate = _normalized_path(file_path)
    trace = _normalized_path(trace_path)
    if repo_root:
        root = _normalized_path(str(repo_root)).rstrip("/") + "/"
        if trace.startswith(root):
            trace = trace[len(root) :]
            if trace == candidate:
                return (3, len(candidate.split("/")))
    candidate_parts = candidate.split("/")
    trace_parts = trace.split("/")
    common = 0
    for left, right in zip(reversed(candidate_parts), reversed(trace_parts)):
        if left != right:
            break
        common += 1
    if common == 0:
        return (0, 0)
    if common == len(candidate_parts) and common > 1:
        return (3, common)
    if common > 1:
        return (2, common)
    return (1, 1)


def _frame_symbol(record: FileRecord, frame: StackFrame) -> SymbolRecord | None:
    if frame.line_number is not None:
        enclosing = [
            symbol
            for symbol in record.symbols
            if symbol.line_start <= frame.line_number <= symbol.line_end
        ]
        if enclosing:
            # Prefer the innermost function over an enclosing class or function.
            return min(
                enclosing,
                key=lambda symbol: (
                    symbol.line_end - symbol.line_start,
                    symbol.kind != "function",
                ),
            )
    if frame.function_name and frame.function_name != "<module>":
        named = [symbol for symbol in record.symbols if symbol.name == frame.function_name]
        if len(named) == 1:
            return named[0]
    return None


def select_stack_location(
    record: FileRecord,
    frames: list[StackFrame],
    repo_root: Path,
) -> tuple[StackFrame | None, SymbolRecord | None]:
    matches = [
        (stack_path_match(record.file_path, frame.file_path, repo_root), frame) for frame in frames
    ]
    matches = [(quality, frame) for quality, frame in matches if quality[0] > 0]
    if not matches:
        return None, None
    # Python tracebacks put the innermost call last; other traces usually put it first.
    if record.language == "python":
        matches.reverse()
    _, frame = max(
        matches,
        key=lambda item: (
            item[0],
            _frame_symbol(record, item[1]) is not None,
        ),
    )
    return frame, _frame_symbol(record, frame)
