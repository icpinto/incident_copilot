from typing import Any, TypedDict


class DiagnosisState(TypedDict, total=False):
    incident_id: str
    parsed_log: dict[str, Any]
    candidates: list[dict[str, Any]]
    deep_analysis: dict[str, Any]
    hypotheses: list[dict[str, Any]]
    likely_service: str | None
    likely_error_class: str | None
    summary: str
    engine: str
    fallback_reason: str | None
    candidate_limit: int
