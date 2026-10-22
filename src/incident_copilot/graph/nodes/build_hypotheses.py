import json
from typing import Any, Dict, List

from incident_copilot.graph.state import DiagnosisState
from incident_copilot.llm.client import get_client
from incident_copilot.llm.prompts import DIAGNOSIS_SYSTEM_PROMPT, DIAGNOSIS_USER_TEMPLATE
from incident_copilot.llm.structured_output import try_parse_json
from incident_copilot.schemas.diagnosis import Hypothesis


def _fallback_hypotheses(state: DiagnosisState) -> List[Dict[str, Any]]:
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
                "next_checks": ["Provide a fuller stack trace", "Specify the failing service explicitly"],
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


async def build_hypotheses_node(state: DiagnosisState, runtime_settings=None) -> DiagnosisState:
    state["engine"] = "deterministic"
    parsed = state.get("parsed_log", {})
    candidates = state.get("candidates", [])
    deep_analysis = state.get("deep_analysis", {})
    if not candidates:
        state["hypotheses"] = _fallback_hypotheses(state)
        return state

    client = get_client(runtime_settings)
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
    )
    try:
        text = await client.complete(prompt=prompt, system_prompt=DIAGNOSIS_SYSTEM_PROMPT)
        data = try_parse_json(text)
        hypotheses = data.get("hypotheses") if isinstance(data, dict) else data
        if not isinstance(hypotheses, list) or not hypotheses:
            raise ValueError("No valid hypotheses returned")
        state["hypotheses"] = [Hypothesis.model_validate(item).model_dump() for item in hypotheses]
        state["engine"] = "deterministic+llm"
    except Exception:
        state["hypotheses"] = _fallback_hypotheses(state)
    return state
