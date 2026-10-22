from typing import Any, Dict, List, Optional, TypedDict


class DiagnosisState(TypedDict, total=False):
    incident_id: str
    parsed_log: Dict[str, Any]
    candidates: List[Dict[str, Any]]
    deep_analysis: Dict[str, Any]
    hypotheses: List[Dict[str, Any]]
    likely_service: Optional[str]
    likely_error_class: Optional[str]
    summary: str
    engine: str
    candidate_limit: int
