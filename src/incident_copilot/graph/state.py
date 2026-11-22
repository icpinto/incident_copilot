from typing import Any, TypedDict


class DiagnosisState(TypedDict, total=False):
    reasoning: dict[str, Any] | None
    reasoning_draft: dict[str, Any]
    incident_id: str
    action_plan_version: int | None
    runbooks: list[dict[str, Any]]
    recommendation_limitations: list[str]
    parsed_log: dict[str, Any]
    incident_context: dict[str, Any] | None
    omitted_initial_context_count: int
    candidates: list[dict[str, Any]]
    deep_analysis: dict[str, Any]
    graph_context: dict[str, Any] | None
    runtime_config: dict[str, Any] | None
    runtime_evidence: list[dict[str, Any]]
    runtime_collection: list[dict[str, Any]]
    runtime_scope: dict[str, Any] | None
    source_unavailable: bool
    hypotheses: list[dict[str, Any]]
    diagnosis_status: str
    diagnosis_reason: str | None
    next_checks: list[str]
    likely_service: str | None
    likely_error_class: str | None
    summary: str
    engine: str
    fallback_reason: str | None
    validation_errors: list[dict[str, Any]]
    guardrails: dict[str, Any] | None
    candidate_limit: int
    execution_id: str
    source_snapshot: dict[str, Any]
    evidence_records: dict[str, dict[str, Any]]
    working_hypotheses: list[dict[str, Any]]
    pending_requests: list[dict[str, Any]]
    retrieval_outcomes: dict[str, dict[str, Any]]
    assessed_request_ids: list[str]
    execution_budget: dict[str, Any]
    investigation_progress: dict[str, Any]
    assessment_decision: dict[str, Any]
    active_request: dict[str, Any] | None
    action_result: dict[str, Any] | None
    investigation: dict[str, Any] | None
    retrieved_evidence: list[dict[str, Any]]
    investigation_events: list[dict[str, Any]]
