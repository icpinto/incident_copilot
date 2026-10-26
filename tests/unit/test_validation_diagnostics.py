import json

import pytest
from pydantic import BaseModel, ValidationError

from incident_copilot.dashboard.models import IncidentEvidence
from incident_copilot.graph.investigation import InvestigationDecision
from incident_copilot.llm.validation import (
    AnalysisValidationFailure,
    schema_diagnostic,
    validate_response,
)
from incident_copilot.schemas.diagnosis import DiagnosisReport


def test_diagnostics_bound_errors_and_hide_extra_field_names_and_values():
    class Contract(BaseModel):
        values: list[int]

    try:
        Contract.model_validate({"values": ["private-sentinel"] * 100}, strict=True)
    except ValidationError as exc:
        diagnostic = schema_diagnostic(exc, Contract, stage="hypothesis_generation")
    assert diagnostic.total_issues == 100
    assert len(diagnostic.issues) == 30
    assert diagnostic.issues[0].path == ["values", 0]
    assert "private-sentinel" not in diagnostic.model_dump_json()

    with pytest.raises(AnalysisValidationFailure) as failure:
        validate_response(json.dumps({"private-sentinel": "private-value"}), InvestigationDecision,
                          stage="investigation_assessment", model_call=1)
    diagnostic = failure.value.diagnostic
    assert any(issue.path == ["[unexpected field]"] and issue.code == "extra_forbidden"
               for issue in diagnostic.issues)
    assert "private-sentinel" not in diagnostic.model_dump_json()
    assert "private-value" not in diagnostic.model_dump_json()


def test_historical_payloads_default_to_no_validation_errors():
    report = DiagnosisReport(incident_id="old", likely_service=None, likely_error_class=None,
                             candidates=[], hypotheses=[], summary="old")
    payload = report.model_dump(exclude={"validation_errors"})
    assert DiagnosisReport.model_validate(payload).validation_errors == []
    assert IncidentEvidence.model_validate({}).validation_errors == []
