import asyncio
import json
import logging

import httpx
import pytest

from incident_copilot.analysis.initial_evidence import initial_evidence
from incident_copilot.core.settings import Settings
from incident_copilot.graph.diagnosis_graph import run_diagnosis_graph
from incident_copilot.graph.nodes import build_hypotheses
from incident_copilot.llm.adapters.ollama import OllamaClient
from incident_copilot.llm.adapters.openai_compatible import OpenAICompatibleClient
from incident_copilot.reporting.markdown_renderer import render_markdown
from incident_copilot.reporting.report_builder import build_report
from tests.action_plan_fixtures import action_plan

OUTCOME = {
    "diagnosis_status": "provisional",
    "diagnosis_reason": "The incident evidence supports this provisional causal mechanism.",
    "next_checks": ["Verify against the failing runtime input"],
}


def hypothesis(rank=1, **overrides):
    return {
        "rank": rank,
        "title": "Payment validation failed",
        "explanation": "charge raised ValueError",
        "evidence": ["main.py:6: ValueError"],
        "confidence": 0.7,
        "next_checks": ["Inspect charge input"],
        "action_plan": action_plan([next(iter(initial_evidence(state(), {"repository": "", "revision": ""})[0]))]),
        **overrides,
    }


def state():
    return {
        "incident_id": "test-incident",
        "parsed_log": {"exception_types": ["ValueError"]},
        "candidates": [{"file_path": "main.py", "score": 20, "reasons": ["stack frame match"]}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["ollama", "openai_compatible", "litellm_gateway"])
async def test_adapters_send_schema_and_graph_keeps_valid_llm_diagnosis(monkeypatch, provider):
    # Exercise the actual adapters and graph against an in-process HTTP transport.
    output = json.dumps({**OUTCOME, "hypotheses": [hypothesis(2), hypothesis(1)]})
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if provider == "ollama":
            assert request.url.path == "/api/generate"
            schema = payload["format"]
            prompt = payload["prompt"]
            assert payload["system"]
            response = {"response": output}
        else:
            assert request.url.path == "/v1/chat/completions"
            assert request.headers["Authorization"] == "Bearer test-key"
            assert payload["model"] == ("incident-diagnosis" if provider == "litellm_gateway" else "test-model")
            assert payload["temperature"] == 0
            response_format = payload["response_format"]
            assert response_format["type"] == "json_schema"
            assert response_format["json_schema"]["strict"] is True
            schema = response_format["json_schema"]["schema"]
            prompt = payload["messages"][1]["content"]
            response = {"choices": [{"message": {"content": output}}]}
        assert schema["type"] == "object"
        assert set(schema["required"]) == {"hypotheses", "diagnosis_status", "diagnosis_reason", "next_checks"}
        assert schema["additionalProperties"] is False
        assert schema["properties"]["hypotheses"]["minItems"] == 0
        assert schema["properties"]["hypotheses"]["maxItems"] == 3
        item = schema["$defs"]["GeneratedHypothesis"]
        assert item["additionalProperties"] is False
        assert set(item["required"]) == set(item["properties"])
        assert set(item["required"]) == {
            "rank",
            "title",
            "explanation",
            "evidence",
            "confidence",
            "next_checks",
            "failure_layer",
            "evidence_refs",
            "action_plan",
        }
        assert item["properties"]["confidence"]["maximum"] == 1
        assert '"hypotheses"' in prompt and "rank (integer)" in prompt
        return httpx.Response(200, json=response)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(handle),
            **kwargs,
        ),
    )
    config = Settings(
        _env_file=None, agentic_enabled=False, guardrails_mode="off",
        llm_provider="ollama" if provider == "ollama" else "openai_compatible",
        ollama_base_url="http://model.test", ollama_model="test-model",
        openai_compatible_base_url="http://litellm:4000/v1/" if provider == "litellm_gateway" else "http://model.test/v1",
        openai_compatible_api_key="test-key",
        openai_compatible_model="incident-diagnosis" if provider == "litellm_gateway" else "test-model",
    )
    report = build_report(await run_diagnosis_graph(state(), config))
    assert len(requests) == 1
    assert report.engine == "deterministic+llm"
    assert report.fallback_reason is None
    assert [item.rank for item in report.hypotheses] == [1, 2]
    assert report.diagnosis_status == "provisional"
    assert all(h.confidence is None and h.confidence_status == "unavailable" for h in report.hypotheses)
    assert [h.model_score for h in report.hypotheses] == [0.7, 0.7]


INVALID_RESPONSES = [
    "Here is my diagnosis: private-key-sentinel",
    '```json\n{"hypotheses": []}\n```',
    json.dumps([hypothesis()]),
    json.dumps({**OUTCOME, "hypotheses": []}),
    json.dumps({**OUTCOME, "hypotheses": [{k: v for k, v in hypothesis().items() if k != "rank"}]}),
    json.dumps({**OUTCOME, "hypotheses": [{k: v for k, v in hypothesis().items() if k != "confidence"}]}),
    json.dumps({**OUTCOME, "hypotheses": [hypothesis(rank="1")]}),
    json.dumps({**OUTCOME, "hypotheses": [hypothesis(rank=True)]}),
    json.dumps({**OUTCOME, "hypotheses": [hypothesis(confidence="0.7")]}),
    json.dumps({**OUTCOME, "hypotheses": [hypothesis(confidence=1.1)]}),
    json.dumps({**OUTCOME, "hypotheses": [hypothesis(confidence=-0.1)]}),
    json.dumps({**OUTCOME, "hypotheses": [hypothesis(confidence=float("nan"))]}),
    json.dumps({**OUTCOME, "hypotheses": [hypothesis(extra="private-key-sentinel")]}),
    json.dumps({**OUTCOME, "hypotheses": [hypothesis(evidence_refs=["uncollected-id"])]}),
    json.dumps({**OUTCOME, "hypotheses": [hypothesis()], "extra": "private-key-sentinel"}),
    json.dumps({**OUTCOME, "hypotheses": [hypothesis(), hypothesis()]}),
    json.dumps({**OUTCOME, "hypotheses": [hypothesis(2)]}),
    json.dumps({**OUTCOME, "hypotheses": [hypothesis(0)]}),
    json.dumps({**OUTCOME, "hypotheses": [hypothesis(4)]}),
    json.dumps({**OUTCOME, "hypotheses": [hypothesis(i) for i in range(1, 5)]}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("output", INVALID_RESPONSES)
async def test_invalid_output_falls_back_and_records_safe_reason(monkeypatch, caplog, output):
    class Model:
        async def complete(self, **kwargs):
            return output

    monkeypatch.setattr(build_hypotheses, "get_client", lambda *_, **_kwargs: Model())
    with caplog.at_level(logging.WARNING):
        report = build_report(await run_diagnosis_graph(state()))
    assert report.engine == "deterministic"
    assert report.fallback_reason == "llm_invalid_response"
    assert report.validation_errors
    assert report.validation_errors[0].stage == "hypothesis_generation"
    assert report.validation_errors[0].model_call == 1
    assert report.hypotheses == []
    assert report.diagnosis_status == "insufficient_evidence"
    assert report.next_checks
    assert "llm_invalid_response" in render_markdown(report)
    assert "llm_invalid_response" in caplog.text
    assert "private-key-sentinel" not in caplog.text
    assert "private-key-sentinel" not in report.model_dump_json()
    assert "Analysis Validation Errors" in render_markdown(report)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["client_creation", "request"])
async def test_request_failure_does_not_expose_error_details(monkeypatch, caplog, failure_at):
    class Model:
        async def complete(self, **kwargs):
            raise ConnectionError("private-key-sentinel")

    def client(*_, **_kwargs):
        if failure_at == "client_creation":
            raise ValueError("private-key-sentinel")
        return Model()

    monkeypatch.setattr(build_hypotheses, "get_client", client)
    with caplog.at_level(logging.WARNING):
        report = build_report(await run_diagnosis_graph(state()))
    assert report.engine == "deterministic"
    assert report.fallback_reason == "llm_request_failed"
    assert "llm_request_failed" in caplog.text
    assert "private-key-sentinel" not in report.model_dump_json() + caplog.text


@pytest.mark.asyncio
async def test_no_candidates_skips_model(monkeypatch):
    def unexpected_client(*_, **_kwargs):
        pytest.fail("LLM should not run without candidates")

    monkeypatch.setattr(build_hypotheses, "get_client", unexpected_client)
    report = build_report(await run_diagnosis_graph({"incident_id": "test", "candidates": []}))
    assert report.engine == "deterministic"
    assert report.fallback_reason == "no_candidates"
    assert report.hypotheses == [] and report.diagnosis_status == "insufficient_evidence"
    assert report.next_checks


@pytest.mark.asyncio
async def test_exact_stack_path_is_localization_only(monkeypatch):
    from incident_copilot.correlation.scorer import score_file
    from incident_copilot.schemas.index import FileRecord
    from incident_copilot.schemas.logs import ParsedLog, StackFrame

    parsed = ParsedLog(raw_text="", stack_frames=[StackFrame(file_path="services/api/main.py", raw="frame")])
    score, reasons = score_file(FileRecord(file_path="services/api/main.py"), parsed)
    assert score == 32

    def offline(*_, **_kwargs):
        raise ConnectionError()

    monkeypatch.setattr(build_hypotheses, "get_client", offline)
    payload = {**state(), "candidates": [{"file_path": "services/api/main.py", "score": score, "reasons": reasons}]}
    report = build_report(await run_diagnosis_graph(payload))
    assert report.candidates[0].score == 32 and report.candidates[0].reasons == reasons
    assert report.hypotheses == []
    assert report.diagnosis_status == "insufficient_evidence"
    markdown = render_markdown(report)
    assert "Insufficient evidence" in markdown and "Localization strength: 32" in markdown
    assert "strong candidate" not in report.summary and "85%" not in markdown


@pytest.mark.asyncio
@pytest.mark.parametrize("tentative_leads", [False, True])
async def test_explicit_abstention_preserves_optional_leads(monkeypatch, tentative_leads):
    class Model:
        async def complete(self, **kwargs):
            return json.dumps({"diagnosis_status": "insufficient_evidence",
                               "diagnosis_reason": "Need the failing runtime input to establish a cause.",
                               "next_checks": ["Capture runtime input"],
                               "hypotheses": [hypothesis()] if tentative_leads else []})

    monkeypatch.setattr(build_hypotheses, "get_client", lambda *_, **_kwargs: Model())
    report = build_report(await run_diagnosis_graph(state()))
    assert report.engine == "deterministic+llm" and report.fallback_reason is None
    assert report.diagnosis_status == "insufficient_evidence"
    assert len(report.hypotheses) == int(tentative_leads)
    assert report.next_checks == ["Capture runtime input"]
    assert all(h.confidence is None and h.model_score == .7 for h in report.hypotheses)


@pytest.mark.asyncio
async def test_cancellation_is_not_converted_to_fallback(monkeypatch):
    class Model:
        async def complete(self, **kwargs):
            raise asyncio.CancelledError()

    monkeypatch.setattr(build_hypotheses, "get_client", lambda *_, **_kwargs: Model())
    with pytest.raises(asyncio.CancelledError):
        await build_hypotheses.build_hypotheses_node(state())


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
@pytest.mark.parametrize("status", [400, 401, 429, 503])
async def test_endpoint_failure_falls_back_safely(monkeypatch, caplog, provider, status):
    def handle(request):
        return httpx.Response(status, json={"error": "provider failure: private-key-sentinel"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(handle),
            **kwargs,
        ),
    )
    client = (
        OllamaClient("http://model.test", "test-model")
        if provider == "ollama"
        else OpenAICompatibleClient("http://model.test/v1", "test-key", "test-model")
    )
    monkeypatch.setattr(build_hypotheses, "get_client", lambda *_, **_kwargs: client)
    with caplog.at_level(logging.WARNING):
        report = build_report(await run_diagnosis_graph(state()))
    assert report.fallback_reason == "llm_request_failed"
    assert report.engine == "deterministic"
    assert "private-key-sentinel" not in report.model_dump_json() + caplog.text
