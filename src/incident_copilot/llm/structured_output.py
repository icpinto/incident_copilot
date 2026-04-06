import json
from typing import Any


def try_parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None
