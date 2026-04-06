import re
from typing import List

from incident_copilot.schemas.logs import ParsedLog

from .error_signature import extract_keywords, extract_table_like_entities
from .stacktrace_parser import parse_stack_frames

SERVICE_RE = re.compile(r'\b([a-zA-Z0-9_-]+)\s+\|\s')
EXCEPTION_RE = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception))\b')


def parse_logs(log_text: str) -> ParsedLog:
    services = sorted(set(SERVICE_RE.findall(log_text)))
    exception_types = sorted(set(EXCEPTION_RE.findall(log_text)))
    stack_frames = parse_stack_frames(log_text)
    error_lines: List[str] = []
    for line in log_text.splitlines():
        lowered = line.lower()
        if any(token in lowered for token in ["error", "exception", "traceback", "failed", "panic", "fatal"]):
            error_lines.append(line.strip())
    return ParsedLog(
        services=services,
        error_lines=error_lines[:50],
        exception_types=exception_types,
        stack_frames=stack_frames,
        keywords=extract_keywords(log_text),
        tables_or_entities=extract_table_like_entities(log_text),
        raw_text=log_text,
    )
