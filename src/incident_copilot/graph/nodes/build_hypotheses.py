import json
import logging

from incident_copilot.core.settings import settings
from incident_copilot.correlation.log_parser import primary_log, scope_notice
from incident_copilot.graph.state import DiagnosisState
from incident_copilot.llm.client import get_client
from incident_copilot.llm.guardrails import GuardrailContext, GuardrailFailure
from incident_copilot.llm.prompts import DIAGNOSIS_SYSTEM_PROMPT, DIAGNOSIS_USER_TEMPLATE
from incident_copilot.llm.structured_output import HypothesesResponse, report_hypothesis
from incident_copilot.llm.validation import AnalysisValidationFailure, validate_response

logger = logging.getLogger(__name__)


def fallback_diagnosis(state: DiagnosisState) -> dict:
    candidates = state.get("candidates", [])
    return {
        "hypotheses": [],
        "diagnosis_status": "insufficient_evidence",
        "diagnosis_reason": (
            "Source correlation identifies locations to investigate but does not establish a cause."
            if candidates else "The available evidence does not identify a source location or cause."
        ),
        "next_checks": (
            [f"Inspect {candidates[0].get('file_path')}",
             "Validate the referenced symbol or configuration against the failing runtime input"]
            if candidates else ["Provide a fuller stack trace", "Specify the failing service explicitly"]
        ),
    }


def build_diagnosis_prompt(state: DiagnosisState, *, policy_view: dict | None = None) -> str:
    original = state.get("parsed_log", {})
    parsed = primary_log(original)
    context = state.get("incident_context") or original.get("incident_context")
    candidates = state.get("candidates", [])
    deep_analysis = state.get("deep_analysis", {})
    response_schema = HypothesesResponse.model_json_schema()
    content = {
        "log_summary": {
            "services": parsed.get("services", []),
            "exception_types": parsed.get("exception_types", []),
            "error_lines": parsed.get("error_lines", [])[:10],
            "keywords": parsed.get("keywords", [])[:20],
            "analysis_scope": scope_notice(context),
            "primary_events": _prompt_events(context),
        },
        "candidate_summary": candidates,
        "deep_analysis": deep_analysis,
        "graph_context": state.get("graph_context"),
    }
    if policy_view is not None:
        policy_view.update(content)
    prompt = DIAGNOSIS_USER_TEMPLATE.format(
        **{key: json.dumps(value, indent=2) for key, value in content.items()},
        response_schema=json.dumps(response_schema, indent=2),
    )
    return prompt


def _prompt_events(context: dict | None) -> dict:
    if not context:
        return {}
    from incident_copilot.correlation.log_parser import group_events
    from incident_copilot.schemas.logs import IncidentContext
    events = group_events(IncidentContext.model_validate(context), context.get("primary_group_id"))
    # Keep single-pass timeline context bounded as well as the existing summary.
    selected, size = [], 0
    for event in events:
        item = event.model_dump()
        item["message"] = item["message"][:2000]
        length = len(json.dumps(item).encode())
        if size + length > 8192:
            continue
        selected.append(item)
        size += length
    return {"events": selected, "omitted_event_count": len(events) - len(selected),
            "limitations": context.get("limitations", [])[:10], "messages_may_be_clipped": True}


async def build_hypotheses_node(state: DiagnosisState, runtime_settings=None,
                                runtime_context=None) -> DiagnosisState:
    state["engine"] = "deterministic"
    state["fallback_reason"] = None
    state["validation_errors"] = []
    state["guardrails"] = None
    candidates = state.get("candidates", [])
    if not candidates:
        state["fallback_reason"] = "no_candidates"
        state.update(fallback_diagnosis(state))
        return state

    response_schema = HypothesesResponse.model_json_schema()
    config = runtime_settings or settings
    context = GuardrailContext(config) if config.guardrails_mode != "off" else None
    view = {}
    prompt = build_diagnosis_prompt(state, policy_view=view)
    if context:
        context.input_text = json.dumps(view, ensure_ascii=False)
        context.model_call = 1

        async def progress(summary):
            state["guardrails"] = summary
            if runtime_context is not None:
                if runtime_context.heartbeat:
                    runtime_context.heartbeat()
                if runtime_context.progress:
                    await runtime_context.progress({"guardrails": summary})

        context.on_update = progress
    try:
        client = (get_client(runtime_settings, guardrail_context=context) if context
                  else get_client(runtime_settings))
        text = await client.complete(
            prompt=prompt,
            system_prompt=DIAGNOSIS_SYSTEM_PROMPT,
            response_schema=response_schema,
        )
    except GuardrailFailure as exc:
        state["fallback_reason"] = exc.reason
        state.update(fallback_diagnosis(state))
        return state
    except Exception:  # noqa: BLE001 -- Provider failures must preserve deterministic diagnosis.
        # Exception messages may contain credentials, prompts, or response bodies.
        logger.warning("LLM diagnosis fallback: llm_request_failed")
        state["fallback_reason"] = "llm_request_failed"
        state.update(fallback_diagnosis(state))
        return state
    try:
        response = validate_response(text, HypothesesResponse, stage="hypothesis_generation", model_call=1)
    except AnalysisValidationFailure as exc:
        logger.warning("LLM diagnosis fallback: llm_invalid_response")
        state["fallback_reason"] = "llm_invalid_response"
        state["validation_errors"] = [exc.diagnostic.model_dump(mode="json")]
        state.update(fallback_diagnosis(state))
    else:
        state.update(response.model_dump(exclude={"hypotheses"}))
        state["hypotheses"] = [report_hypothesis(item.model_dump()) for item in response.hypotheses]
        state["engine"] = "deterministic+llm"
    return state
