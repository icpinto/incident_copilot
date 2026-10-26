from uuid import uuid4

from incident_copilot.schemas.candidates import CandidateLocation
from incident_copilot.schemas.diagnosis import DiagnosisReport, Hypothesis


def build_report(graph_state: dict) -> DiagnosisReport:
    return DiagnosisReport(
        incident_id=graph_state.get("incident_id") or str(uuid4()),
        likely_service=graph_state.get("likely_service"),
        likely_error_class=graph_state.get("likely_error_class"),
        candidates=[CandidateLocation.model_validate(c) for c in graph_state.get("candidates", [])],
        hypotheses=[Hypothesis.model_validate(h) for h in graph_state.get("hypotheses", [])],
        summary=graph_state.get("summary", "No summary generated."),
        incident_context=graph_state.get("incident_context"),
        engine=graph_state.get("engine", "deterministic"),
        fallback_reason=graph_state.get("fallback_reason"),
        validation_errors=graph_state.get("validation_errors", []),
        graph_context=graph_state.get("graph_context"),
        retrieved_evidence=graph_state.get("retrieved_evidence", []),
        investigation=graph_state.get("investigation"),
        investigation_events=graph_state.get("investigation_events", []),
    )
