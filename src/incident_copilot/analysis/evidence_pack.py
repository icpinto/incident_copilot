from typing import Any, Dict, List

from incident_copilot.schemas.candidates import CandidateLocation
from incident_copilot.schemas.logs import ParsedLog


def build_evidence_pack(parsed_log: ParsedLog, candidates: List[CandidateLocation], deep_analysis: dict) -> Dict[str, Any]:
    return {
        "services": parsed_log.services,
        "exception_types": parsed_log.exception_types,
        "error_lines": parsed_log.error_lines[:10],
        "keywords": parsed_log.keywords[:20],
        "stack_frames": [f.model_dump() for f in parsed_log.stack_frames[:10]],
        "candidates": [c.model_dump() for c in candidates],
        "deep_analysis": deep_analysis,
    }
