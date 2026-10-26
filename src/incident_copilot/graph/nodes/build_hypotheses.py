import json
import logging
from typing import Any

from incident_copilot.graph.state import DiagnosisState
from incident_copilot.llm.client import get_client
from incident_copilot.llm.prompts import DIAGNOSIS_SYSTEM_PROMPT, DIAGNOSIS_USER_TEMPLATE
from incident_copilot.llm.structured_output import HypothesesResponse
from incident_copilot.llm.validation import AnalysisValidationFailure, validate_response

logger = logging.getLogger(__name__)


def _fallback_hypotheses(state: DiagnosisState) -> list[dict[str, Any]]:
    parsed = state.get("parsed_log", {})
    candidates = state.get("candidates", [])
    if not candidates:
        return [
            {
                "rank": 1,
                "title": "Insufficient localization",
                "explanation": "The system could not localize the incident to a specific file with confidence.",
                "evidence": parsed.get("error_lines", [])[:3],
                "confidence": 0.15,
                "next_checks": [
                    "Provide a fuller stack trace",
                    "Specify the failing service explicitly",
                ],
            }
        ]
    top = candidates[0]
    return [
        {
            "rank": 1,
            "title": f"Likely issue in {top.get('file_path')}",
            "explanation": "Deterministic correlation signals point to this file as the strongest candidate.",
            "evidence": top.get("reasons", [])[:5],
            "confidence": min(0.85, top.get("score", 0) / 20.0),
            "next_checks": [
                f"Inspect {top.get('file_path')}",
                "Validate the referenced symbol or configuration path",
            ],
        }
    ]


def build_diagnosis_prompt(state: DiagnosisState) -> str:
    parsed = state.get("parsed_log", {})
    candidates = state.get("candidates", [])
    deep_analysis = state.get("deep_analysis", {})
    response_schema = HypothesesResponse.model_json_schema()
    prompt = DIAGNOSIS_USER_TEMPLATE.format(
        log_summary=json.dumps(
            {
                "services": parsed.get("services", []),
                "exception_types": parsed.get("exception_types", []),
                "error_lines": parsed.get("error_lines", [])[:10],
                "keywords": parsed.get("keywords", [])[:20],
            },
            indent=2,
        ),
        candidate_summary=json.dumps(candidates, indent=2),
        deep_analysis=json.dumps(deep_analysis, indent=2),
        graph_context=json.dumps(state.get("graph_context"), indent=2),
        response_schema=json.dumps(response_schema, indent=2),
    )
    return prompt


async def build_hypotheses_node(state: DiagnosisState, runtime_settings=None) -> DiagnosisState:
    state["engine"] = "deterministic"
    state["fallback_reason"] = None
    state["validation_errors"] = []
    candidates = state.get("candidates", [])
    if not candidates:
        state["fallback_reason"] = "no_candidates"
        state["hypotheses"] = _fallback_hypotheses(state)
        return state

    response_schema = HypothesesResponse.model_json_schema()
    prompt = build_diagnosis_prompt(state)
    try:
        client = get_client(runtime_settings)
        text = await client.complete(
            prompt=prompt,
            system_prompt=DIAGNOSIS_SYSTEM_PROMPT,
            response_schema=response_schema,
        )
    except Exception:  # noqa: BLE001 -- Provider failures must preserve deterministic diagnosis.
        # Exception messages may contain credentials, prompts, or response bodies.
        logger.warning("LLM diagnosis fallback: llm_request_failed")
        state["fallback_reason"] = "llm_request_failed"
        state["hypotheses"] = _fallback_hypotheses(state)
        return state
    try:
        response = validate_response(text, HypothesesResponse, stage="hypothesis_generation", model_call=1)
    except AnalysisValidationFailure as exc:
        logger.warning("LLM diagnosis fallback: llm_invalid_response")
        state["fallback_reason"] = "llm_invalid_response"
        state["validation_errors"] = [exc.diagnostic.model_dump(mode="json")]
        state["hypotheses"] = _fallback_hypotheses(state)
    else:
        state["hypotheses"] = [item.model_dump() for item in response.hypotheses]
        state["engine"] = "deterministic+llm"
    return state
