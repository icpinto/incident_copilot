from pathlib import Path
from typing import Iterable, List

from incident_copilot.schemas.candidates import CandidateLocation
from incident_copilot.tools.file_reader import read_snippet


def build_candidate_snippets(repo_root: Path, candidates: Iterable[CandidateLocation]) -> List[dict]:
    result: List[dict] = []
    for c in candidates:
        path = repo_root / c.file_path
        snippet = c.snippet
        if snippet is None and path.exists():
            snippet = read_snippet(path, c.line_start, c.line_end)
        result.append(
            {
                "file_path": c.file_path,
                "symbol_name": c.symbol_name,
                "score": c.score,
                "reasons": c.reasons,
                "snippet": snippet,
            }
        )
    return result
