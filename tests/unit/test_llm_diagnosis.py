import asyncio
import json
import logging

import httpx
import pytest

from incident_copilot.graph.diagnosis_graph import run_diagnosis_graph
from incident_copilot.graph.nodes import build_hypotheses
from incident_copilot.llm.adapters.ollama import OllamaClient
from incident_copilot.llm.adapters.openai_compatible import OpenAICompatibleClient
from incident_copilot.reporting.markdown_renderer import render_markdown
from incident_copilot.reporting.report_builder import build_report


def hypothesis(rank=1, **overrides):
    return {
        "rank": rank,
        "title": "Payment validation failed",
        "explanation": "charge raised ValueError",
        "evidence": ["main.py:6: ValueError"],
        "confidence": 0.7,
        "next_checks": ["Inspect charge input"],
        **overrides,
    }


def state():
    return {
        "incident_id": "test-incident",
        "parsed_log": {"exception_types": ["ValueError"]},
        "candidates": [{"file_path": "main.py", "score": 20, "reasons": ["stack frame match"]}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
async def test_adapters_send_schema_and_graph_keeps_valid_llm_diagnosis(monkeypatch, provider):
    # Exercise the actual adapters and graph against an in-process HTTP transport.
    output = json.dumps({"hypotheses": [hypothesis(2), hypothesis(1)]})
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
            response_format = payload["response_format"]
            assert response_format["type"] == "json_schema"
            assert response_format["json_schema"]["strict"] is True
            schema = response_format["json_schema"]["schema"]
            prompt = payload["messages"][1]["content"]
            response = {"choices": [{"message": {"content": output}}]}
        assert schema["type"] == "object"
        assert schema["required"] == ["hypotheses"]
        assert schema["additionalProperties"] is False
        assert schema["properties"]["hypotheses"]["minItems"] == 1
        assert schema["properties"]["hypotheses"]["maxItems"] == 3
        item = schema["$defs"]["Hypothesis"]
        assert item["additionalProperties"] is False
        assert set(item["required"]) == set(item["properties"])
        assert set(item["required"]) == {
            "rank",
            "title",
            "explanation",
            "evidence",
            "confidence",
            "next_checks",
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
    client = (
        OllamaClient("http://model.test", "test-model")
        if provider == "ollama"
        else OpenAICompatibleClient("http://model.test/v1", "test-key", "test-model")
    )
    monkeypatch.setattr(build_hypotheses, "get_client", lambda *_: client)
    report = build_report(await run_diagnosis_graph(state()))
    assert len(requests) == 1
    assert report.engine == "deterministic+llm"
    assert report.fallback_reason is None
    assert [item.rank for item in report.hypotheses] == [1, 2]


INVALID_RESPONSES = [
    "Here is my diagnosis: private-key-sentinel",
    '```json\n{"hypotheses": []}\n```',
    json.dumps([hypothesis()]),
    json.dumps({"hypotheses": []}),
    json.dumps({"hypotheses": [{k: v for k, v in hypothesis().items() if k != "rank"}]}),
    json.dumps({"hypotheses": [{k: v for k, v in hypothesis().items() if k != "confidence"}]}),
    json.dumps({"hypotheses": [hypothesis(rank="1")]}),
    json.dumps({"hypotheses": [hypothesis(rank=True)]}),
    json.dumps({"hypotheses": [hypothesis(confidence="0.7")]}),
    json.dumps({"hypotheses": [hypothesis(confidence=1.1)]}),
    json.dumps({"hypotheses": [hypothesis(confidence=-0.1)]}),
    json.dumps({"hypotheses": [hypothesis(confidence=float("nan"))]}),
    json.dumps({"hypotheses": [hypothesis(extra="private-key-sentinel")]}),
    json.dumps({"hypotheses": [hypothesis(evidence_refs=["uncollected-id"])]}),
    json.dumps({"hypotheses": [hypothesis()], "extra": "private-key-sentinel"}),
    json.dumps({"hypotheses": [hypothesis(), hypothesis()]}),
    json.dumps({"hypotheses": [hypothesis(2)]}),
    json.dumps({"hypotheses": [hypothesis(0)]}),
    json.dumps({"hypotheses": [hypothesis(4)]}),
    json.dumps({"hypotheses": [hypothesis(i) for i in range(1, 5)]}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("output", INVALID_RESPONSES)
async def test_invalid_output_falls_back_and_records_safe_reason(monkeypatch, caplog, output):
    class Model:
        async def complete(self, **kwargs):
            return output

    monkeypatch.setattr(build_hypotheses, "get_client", lambda *_: Model())
    with caplog.at_level(logging.WARNING):
        report = build_report(await run_diagnosis_graph(state()))
    assert report.engine == "deterministic"
    assert report.fallback_reason == "llm_invalid_response"
    assert report.hypotheses[0].title == "Likely issue in main.py"
    assert "llm_invalid_response" in render_markdown(report)
    assert "llm_invalid_response" in caplog.text
    assert "private-key-sentinel" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["client_creation", "request"])
async def test_request_failure_does_not_expose_error_details(monkeypatch, caplog, failure_at):
    class Model:
        async def complete(self, **kwargs):
            raise ConnectionError("private-key-sentinel")

    def client(*_):
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
    def unexpected_client(*_):
        pytest.fail("LLM should not run without candidates")

    monkeypatch.setattr(build_hypotheses, "get_client", unexpected_client)
    report = build_report(await run_diagnosis_graph({"incident_id": "test", "candidates": []}))
    assert report.engine == "deterministic"
    assert report.fallback_reason == "no_candidates"


@pytest.mark.asyncio
async def test_cancellation_is_not_converted_to_fallback(monkeypatch):
    class Model:
        async def complete(self, **kwargs):
            raise asyncio.CancelledError()

    monkeypatch.setattr(build_hypotheses, "get_client", lambda *_: Model())
    with pytest.raises(asyncio.CancelledError):
        await build_hypotheses.build_hypotheses_node(state())


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
async def test_endpoint_rejecting_schema_falls_back_safely(monkeypatch, caplog, provider):
    def handle(request):
        return httpx.Response(400, json={"error": "schema unsupported: private-key-sentinel"})

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
    monkeypatch.setattr(build_hypotheses, "get_client", lambda *_: client)
    with caplog.at_level(logging.WARNING):
        report = build_report(await run_diagnosis_graph(state()))
    assert report.fallback_reason == "llm_request_failed"
    assert report.engine == "deterministic"
    assert "private-key-sentinel" not in report.model_dump_json() + caplog.text
