import re
from typing import List

ERROR_KEYWORD_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
TABLE_RE = re.compile(r'\b([a-z_][a-z0-9_]{2,})\b')


def extract_keywords(text: str) -> List[str]:
    raw = ERROR_KEYWORD_RE.findall(text)
    stop = {
        "the", "and", "for", "from", "that", "this", "with", "error", "traceback",
        "line", "file", "exception", "failed", "service", "container", "docker",
    }
    return sorted({w for w in raw if w.lower() not in stop})[:50]


def extract_table_like_entities(text: str) -> List[str]:
    candidates = []
    for token in TABLE_RE.findall(text):
        if "_" in token:
            candidates.append(token)
    return sorted(set(candidates))[:20]
