from pathlib import Path
from typing import List

from incident_copilot.schemas.candidates import CandidateLocation, CandidateSet
from incident_copilot.tools.file_reader import read_snippet

from incident_copilot.indexing.storage import IndexStorage
from .log_parser import parse_logs
from .scorer import score_file


def rank_candidates(repo_root: Path, index_db_path: Path, log_text: str, top_k: int = 10, user_hint_service: str | None = None) -> tuple[CandidateSet, object]:
    storage = IndexStorage(index_db_path)
    parsed_log = parse_logs(log_text)
    if user_hint_service and user_hint_service not in parsed_log.services:
        parsed_log.services.insert(0, user_hint_service)
    ranked: List[CandidateLocation] = []
    for record in storage.get_file_records():
        score, reasons = score_file(record, parsed_log)
        if score <= 0:
            continue
        symbol_name = record.symbols[0].name if record.symbols else None
        line_start = record.symbols[0].line_start if record.symbols else None
        line_end = record.symbols[0].line_end if record.symbols else None
        abs_path = repo_root / record.file_path
        snippet = read_snippet(abs_path, line_start, line_end) if abs_path.exists() else None
        ranked.append(
            CandidateLocation(
                file_path=record.file_path,
                symbol_name=symbol_name,
                service_name=next((s for s in parsed_log.services if s in record.service_hints), record.service_hints[0] if len(record.service_hints) == 1 else None),
                score=round(score, 2),
                reasons=reasons[:10],
                snippet=snippet,
                line_start=line_start,
                line_end=line_end,
            )
        )
    ranked.sort(key=lambda x: x.score, reverse=True)
    return CandidateSet(items=ranked[:top_k]), parsed_log
