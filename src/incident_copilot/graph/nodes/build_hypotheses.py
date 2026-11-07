import json
import logging

from incident_copilot.analysis.evidence_tools import redact_evidence
from incident_copilot.analysis.initial_evidence import initial_evidence
from incident_copilot.analysis.recommendations import validate_action_plans
from incident_copilot.core.settings import settings
from incident_copilot.correlation.log_parser import primary_log, scope_notice
from incident_copilot.graph.state import DiagnosisState
from incident_copilot.llm.client import get_client
from incident_copilot.llm.guardrails import GuardrailContext, GuardrailFailure
from incident_copilot.llm.prompts import DIAGNOSIS_SYSTEM_PROMPT, DIAGNOSIS_USER_TEMPLATE
from incident_copilot.llm.structured_output import (
    HypothesesResponse,
    hypotheses_schema,
    report_hypothesis,
)
from incident_copilot.llm.validation import AnalysisValidationFailure, validate_response

logger = logging.getLogger(__name__)


def usable_runtime(items):
    for item in items:
        data = item.get("data", {})
        if data.get("status") not in {"ok", "partial"}:
            continue
        if item["kind"] == "runtime_resources":
            if any(row.get("samples") for row in data.get("observations", [])):
                return True
        elif data.get("observations") or data.get("spans") or any(metric.get("values") for metric in data.get("metrics", [])):
            return True
    return False


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
    response_schema = hypotheses_schema()
    records, omitted = initial_evidence(state, state.get("source_snapshot") or {"repository": "", "revision": ""})
    if omitted:
        state["recommendation_limitations"] = state.get("recommendation_limitations", []) + [f"{omitted} initial observations omitted by evidence limits."]
    content = {
        "citable_evidence": list(records.values()),
        "runbooks": state.get("runbooks", []),
        "log_summary": {
            "source_unavailable": bool(state.get("source_unavailable")),
            "services": [str(value)[:120] for value in parsed.get("services", [])[:20]],
            "exception_types": [str(value)[:128] for value in parsed.get("exception_types", [])[:20]],
            "error_lines": [str(value)[:1024] for value in parsed.get("error_lines", [])[:10]],
            "keywords": [str(value)[:128] for value in parsed.get("keywords", [])[:20]],
            "analysis_scope": scope_notice(context),
            "primary_events": _prompt_events(context),
        },
        "candidate_summary": candidates,
        "deep_analysis": deep_analysis,
        "graph_context": state.get("graph_context"),
        "runtime_config": state.get("runtime_config"),
        "runtime_evidence": state.get("runtime_evidence", []),
        "runtime_collection": state.get("runtime_collection", []),
    }
    def render():
        return DIAGNOSIS_USER_TEMPLATE.format(
            **{key: json.dumps(value) for key, value in content.items()},
            response_schema=json.dumps(response_schema),
        )
    prompt = render()
    while len(prompt.encode()) + len(DIAGNOSIS_SYSTEM_PROMPT.encode()) > 32768:
        if content["deep_analysis"]:
            content["deep_analysis"] = dict(content["deep_analysis"])
            content["deep_analysis"].pop(next(reversed(content["deep_analysis"])))
        elif content["graph_context"]:
            content["graph_context"] = None
        elif content["runtime_evidence"]:
            content["runtime_evidence"] = content["runtime_evidence"][:-1]
        elif content["candidate_summary"]:
            content["candidate_summary"] = content["candidate_summary"][:-1]
        elif content["runtime_config"]:
            content["runtime_config"] = None
        elif content["log_summary"]["primary_events"].get("events"):
            content["log_summary"]["primary_events"]["events"].pop()
            content["log_summary"]["primary_events"]["omitted_event_count"] += 1
        elif content["log_summary"]["error_lines"]:
            content["log_summary"]["error_lines"].pop()
        elif content["runtime_collection"]:
            content["runtime_collection"] = content["runtime_collection"][:-1]
        elif content["runbooks"]:
            content["runbooks"] = content["runbooks"][:-1]
            state["recommendation_limitations"] = state.get("recommendation_limitations", []) + ["A matching runbook was omitted by the prompt limit."]
        elif content["citable_evidence"]:
            content["citable_evidence"] = content["citable_evidence"][:-1]
            state["recommendation_limitations"] = state.get("recommendation_limitations", []) + ["An observation was omitted by the prompt limit."]
        else:
            break
        prompt = render()
    if len(prompt.encode()) + len(DIAGNOSIS_SYSTEM_PROMPT.encode()) > 32768:
        from incident_copilot.llm.validation import validation_failure
        raise validation_failure(stage="hypothesis_generation", kind="limit", code="prompt_limit", model_call=1)
    state["retrieved_evidence"] = content["citable_evidence"]
    state["runbooks"] = content["runbooks"]
    state["recommendation_limitations"] = list(dict.fromkeys(state.get("recommendation_limitations", [])))
    if policy_view is not None:
        policy_view.update(content)
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
    config = runtime_settings or settings
    state = redact_evidence(state, config)
    candidates = state.get("candidates", [])
    state["retrieved_evidence"] = state.get("runtime_evidence", [])
    if not candidates and not usable_runtime(state.get("runtime_evidence", [])):
        state["fallback_reason"] = "no_candidates"
        state.update(fallback_diagnosis(state))
        return state

    response_schema = hypotheses_schema()
    config = runtime_settings or settings
    context = GuardrailContext(config) if config.guardrails_mode != "off" else None
    view = {}
    try:
        prompt = build_diagnosis_prompt(state, policy_view=view)
    except AnalysisValidationFailure as exc:
        state["validation_errors"] = [exc.diagnostic.model_dump(mode="json")]
        state["fallback_reason"] = "prompt_limit"
        state.update(fallback_diagnosis(state))
        return state
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
        known = {item["id"] for item in state.get("retrieved_evidence", [])}
        validate_action_plans(response.hypotheses, known, state.get("runbooks", []),
                              stage="hypothesis_generation", model_call=1,
                              diagnosis_status=response.diagnosis_status)
        for index, hypothesis in enumerate(response.hypotheses):
            if set(hypothesis.evidence_refs) - known:
                from incident_copilot.llm.validation import validation_failure
                raise validation_failure(stage="hypothesis_generation", kind="semantic", code="invalid_evidence_reference",
                                         path=["hypotheses", index, "evidence_refs"], model_call=1)
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
