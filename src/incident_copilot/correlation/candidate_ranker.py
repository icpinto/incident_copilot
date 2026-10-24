from pathlib import Path

from incident_copilot.indexing.storage import IndexStorage
from incident_copilot.schemas.candidates import CandidateLocation, CandidateSet
from incident_copilot.schemas.logs import ParsedLog
from incident_copilot.tools.file_reader import read_snippet

from .locations import select_stack_location
from .log_parser import parse_logs
from .scorer import score_file


def rank_candidates(
    repo_root: Path,
    index_db_path: Path,
    log_text: str,
    top_k: int = 10,
    user_hint_service: str | None = None,
) -> tuple[CandidateSet, ParsedLog]:
    storage = IndexStorage(index_db_path)
    parsed_log = parse_logs(log_text)
    if user_hint_service and user_hint_service not in parsed_log.services:
        parsed_log.services.insert(0, user_hint_service)
    ranked: list[CandidateLocation] = []
    for record in storage.get_file_records():
        score, reasons = score_file(record, parsed_log, repo_root)
        if score <= 0:
            continue
        frame, symbol = select_stack_location(record, parsed_log.stack_frames, repo_root)
        symbol_name = symbol.name if symbol else None
        line_start = symbol.line_start if symbol else None
        line_end = symbol.line_end if symbol else None
        if symbol is None and frame and frame.line_number and frame.line_number > 0:
            line_start = max(1, frame.line_number - 5)
            line_end = frame.line_number + 5
        ranked.append(
            CandidateLocation(
                file_path=record.file_path,
                symbol_name=symbol_name,
                service_name=next(
                    (s for s in parsed_log.services if s in record.service_hints),
                    record.service_hints[0] if len(record.service_hints) == 1 else None,
                ),
                score=round(score, 2),
                reasons=reasons[:10],
                line_start=line_start,
                line_end=line_end,
            )
        )
    ranked.sort(key=lambda x: x.score, reverse=True)
    selected = ranked[:top_k]
    for candidate in selected:
        path = repo_root / candidate.file_path
        if path.is_file():
            candidate.snippet = read_snippet(path, candidate.line_start, candidate.line_end)
    return CandidateSet(items=selected), parsed_log
