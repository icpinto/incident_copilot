from pathlib import Path

from incident_copilot.indexing.storage import IndexStorage
from incident_copilot.schemas.candidates import CandidateLocation, CandidateSet
from incident_copilot.schemas.logs import ParsedLog
from incident_copilot.tools.file_reader import read_snippet

from .locations import select_stack_location
from .log_parser import group_events, parse_logs, summarize_events
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
    context = parsed_log.incident_context
    records = storage.get_file_records()
    if context is None or not context.groups:
        return CandidateSet(items=[]), parsed_log
    summaries = {}
    event_map = {event.id: event for event in context.events}
    for group in context.groups:
        summary = summarize_events([event_map[event_id] for event_id in group.event_ids])
        summaries[group.id] = summary
        group.max_candidate_score = round(max(
            (score_file(record, summary, repo_root, user_hint_service)[0] for record in records),
            default=0,
        ), 2)
    eligible = [g for g in context.groups if g.error_bearing] or context.groups
    hinted = [g for g in eligible if user_hint_service in g.services] if user_hint_service else []
    if user_hint_service and not hinted:
        context.limitations.append("Service hint did not match an observed service in eligible groups.")
    pool = hinted or eligible
    primary = min(pool, key=lambda g: (-g.max_candidate_score, g.first_line))
    context.primary_group_id = primary.id
    context.selection_reason = (
        ("Preferred groups containing the observed service hint; " if hinted else "")
        + ("considered error-bearing groups; " if any(g.error_bearing for g in context.groups) else "considered all groups; ")
        + ("selected highest candidate score, with source order breaking ties."
           if primary.max_candidate_score > 0 else "no positive candidate scores; selected earliest eligible group.")
    )
    focused = summaries[primary.id]
    events = group_events(context, primary.id)
    ranked: list[CandidateLocation] = []
    for record in records:
        score, reasons = score_file(record, focused, repo_root, user_hint_service)
        if score <= 0:
            continue
        frame, symbol = select_stack_location(record, focused.stack_frames, repo_root)
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
                    (s for s in focused.services if s in record.service_hints),
                    record.service_hints[0] if len(record.service_hints) == 1 else None,
                ),
                score=round(score, 2),
                reasons=reasons[:10],
                line_start=line_start,
                line_end=line_end,
                group_id=primary.id,
            )
        )
    ranked.sort(key=lambda x: x.score, reverse=True)
    selected = ranked[:top_k]
    event_summaries = [(e.id, summarize_events([e])) for e in events]
    record_map = {record.file_path: record for record in records}
    for candidate in selected:
        record = record_map[candidate.file_path]
        candidate.event_ids = [event_id for event_id, summary in event_summaries
                               if score_file(record, summary, repo_root)[0] > 0]
        path = repo_root / candidate.file_path
        if path.is_file():
            candidate.snippet = read_snippet(path, candidate.line_start, candidate.line_end)
    return CandidateSet(items=selected), parsed_log
