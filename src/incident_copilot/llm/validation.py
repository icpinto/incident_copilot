"""Translate validation failures into bounded, safe, typed analysis diagnostics."""
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from incident_copilot.schemas.validation import (
    AnalysisValidationError,
    ValidationIssue,
    ValidationKind,
    ValidationStage,
)

T = TypeVar("T", bound=BaseModel)

# Custom validators use these stable codes instead of unstructured ValueError text.
SEMANTIC_MESSAGES = {
    "action_plan_required": "New hypotheses require a structured action plan.",
    "invalid_runbook_reference": "Runbook or section was not supplied to the model.",
    "mitigation_requires_diagnosis": "Insufficient-evidence diagnoses require checks before mitigation proposals.",
    "provisional_requires_hypothesis": "A provisional diagnosis requires at least one causal hypothesis.",
    "action_request_mismatch": "Action must be retrieve with requests, or finish without requests.",
    "invalid_ranks": "Hypothesis ranks must be unique and consecutive, starting at 1.",
    "duplicate_hypothesis_id": "Each hypothesis must have a unique ID.",
    "uncollected_evidence": "Single-pass hypotheses cannot cite uncollected evidence.",
    "invalid_evidence_reference": "Citation does not refer to evidence supplied to the model.",
    "query_required": "This evidence tool requires a query.",
    "symbol_required": "This evidence tool requires an exact symbol reference.",
    "file_path_required": "This evidence tool requires a file path.",
    "source_location_required": "A symbol reference or file path is required.",
    "invalid_line_range": "The ending line must be at or after the starting line.",
    "conflicting_source_location": "The file path must match the symbol's file path.",
    "invalid_source_location": "Requested source location is outside the permitted repository or unavailable.",
    "unindexed_source_location": "Requested file is absent from the trusted source index.",
    "unverified_symbol": "Requested symbol does not match a verified indexed symbol and line range.",
    "invalid_evidence_request": "Evidence request does not satisfy the retrieval contract.",
}
MESSAGES = {
    "json_invalid": "Response is not valid JSON.",
    "json_type": "Expected JSON text from the model.",
    "missing": "Required field is missing.",
    "extra_forbidden": "Unexpected field is not allowed by the response schema.",
    "int_type": "Expected an integer.",
    "int_parsing": "Expected an integer.",
    "float_type": "Expected a number.",
    "float_parsing": "Expected a number.",
    "bool_type": "Expected a boolean.",
    "string_type": "Expected a string.",
    "list_type": "Expected an array.",
    "dict_type": "Expected an object.",
    "model_type": "Expected an object matching the response schema.",
    "finite_number": "Expected a finite number.",
    "literal_error": "Value must match one of the schema's allowed values.",
    "enum": "Value must match one of the schema's allowed values.",
    "response_too_large": "Model response exceeds the permitted byte limit.",
    "prompt_limit": "Required investigation context exceeds the model input byte limit.",
    **SEMANTIC_MESSAGES,
}
BOUND_MESSAGES = {
    "greater_than_equal": ("ge", "Value must be at least {bound}."),
    "less_than_equal": ("le", "Value must be at most {bound}."),
    "greater_than": ("gt", "Value must be greater than {bound}."),
    "less_than": ("lt", "Value must be less than {bound}."),
    "too_short": ("min_length", "Expected at least {bound} items."),
    "too_long": ("max_length", "Expected at most {bound} items."),
    "string_too_short": ("min_length", "Expected at least {bound} characters."),
    "string_too_long": ("max_length", "Expected at most {bound} characters."),
}


class AnalysisValidationFailure(Exception):
    def __init__(self, diagnostic: AnalysisValidationError):
        self.diagnostic = diagnostic
        super().__init__(diagnostic.issues[0].code)


def schema_diagnostic(exc: ValidationError, contract: type[BaseModel], *,
                      stage: ValidationStage, model_call: int | None = None,
                      request_id: str | None = None) -> AnalysisValidationError:
    fields: set[str] = set()

    def collect(node):
        if isinstance(node, dict):
            fields.update(node.get("properties", {}))
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(contract.model_json_schema())
    errors = exc.errors(include_url=False, include_input=False, include_context=True)
    issues = []
    for error in errors[:30]:
        code = error["type"]
        message = MESSAGES.get(code, "Value does not satisfy the response schema.")
        if code == "json_invalid":
            # Extract coordinates only; parser text may include rejected input.
            position = re.search(r" at line (\d+) column (\d+)$", error.get("ctx", {}).get("error", ""))
            if position:
                message += f" Line {int(position[1])}, column {int(position[2])}."
        if code in BOUND_MESSAGES:
            key, template = BOUND_MESSAGES[code]
            bound = error.get("ctx", {}).get(key)
            if type(bound) in (int, float):
                message = template.format(bound=bound)
        if code in {"literal_error", "enum"}:
            # This is the contract's fixed set of allowed values, never the input.
            expected = error.get("ctx", {}).get("expected")
            if isinstance(expected, str):
                message = f"Expected {expected}."
        # Extra-field names and exception context may themselves contain secrets.
        path = [part if type(part) is int or part in fields else "[unexpected field]"
                for part in error["loc"]]
        issues.append(ValidationIssue(path=path, code=code, message=message))
    codes = {error["type"] for error in errors}
    kind: ValidationKind = "json" if codes & {"json_invalid", "json_type"} else "semantic" if codes & SEMANTIC_MESSAGES.keys() else "schema"
    return AnalysisValidationError(stage=stage, kind=kind, model_call=model_call,
                                   request_id=request_id, issues=issues, total_issues=len(errors))


def validate_response(response: str, contract: type[T], *, stage: ValidationStage,
                      model_call: int) -> T:
    try:
        return contract.model_validate_json(response, strict=True)
    except ValidationError as exc:
        raise AnalysisValidationFailure(schema_diagnostic(
            exc, contract, stage=stage, model_call=model_call,
        )) from None


def validation_failure(*, stage: ValidationStage, kind: ValidationKind, code: str,
                       path: list[str | int] | None = None,
                       model_call: int | None = None,
                       request_id: str | None = None) -> AnalysisValidationFailure:
    return AnalysisValidationFailure(AnalysisValidationError(
        stage=stage, kind=kind, model_call=model_call, request_id=request_id,
        issues=[ValidationIssue(path=path or [], code=code, message=MESSAGES[code])], total_issues=1,
    ))
