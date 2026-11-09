import asyncio
import json
from uuid import uuid4

import httpx
import pytest

pytest.importorskip("langfuse")
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from incident_copilot.observability import runtime as tracing
from incident_copilot.observability.prompts import Prompt, Section, categorized, manifest


@pytest.fixture
def telemetry(monkeypatch):
    tracing.shutdown()
    exporter = InMemorySpanExporter()
    monkeypatch.setenv("IC_LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("IC_LANGFUSE_CAPTURE_CONTENT", "false")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-" + uuid4().hex)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "observability-test-secret")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://localhost:3000")
    runtime_factory = tracing._safe_exporter
    monkeypatch.setattr(tracing, "_safe_exporter", lambda *args: exporter)
    runtime = tracing.initialize()
    assert runtime is not None
    monkeypatch.setattr(runtime.counter, "submit", lambda *args: None)
    runtime.original_exporter_factory = runtime_factory
    yield runtime, exporter
    tracing.shutdown()


def finished(telemetry):
    runtime, exporter = telemetry
    runtime.provider.force_flush()
    return exporter.get_finished_spans()


def test_disabled_never_constructs_sdk_or_headers(monkeypatch):
    tracing.shutdown()
    monkeypatch.setenv("IC_LANGFUSE_ENABLED", "false")
    monkeypatch.setattr(tracing, "Telemetry", lambda _: pytest.fail("SDK initialized"))
    assert tracing.new_trace("execution") == (None, None)
    with tracing.investigation("execution"):
        assert tracing.trace_headers() == {}
        assert tracing.current_reference() is None
    tracing.shutdown()


def test_sdk_initialization_failure_is_best_effort(monkeypatch):
    tracing.shutdown()
    monkeypatch.setenv("IC_LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "secret")
    def unavailable(_):
        raise ConnectionError("secret-bearing-error")
    monkeypatch.setattr(tracing, "Telemetry", unavailable)
    with tracing.span("still-analyzes"):
        assert tracing.initialize() is None
    tracing.shutdown()


def test_persisted_context_survives_redispatch_and_has_unique_attempts(telemetry):
    reference, carrier = tracing.new_trace("incident-123")
    for attempt in (1, 2):
        with (
            tracing.attach_context(carrier),
            tracing.investigation("incident-123", stage="reasoning", activity_attempt=attempt),
            tracing.call_scope("review", model_call=2),
            tracing.model_request("evidence", "instructions", "alias"),
        ):
            headers = tracing.trace_headers()
            assert headers["traceparent"].split("-")[1] == reference["trace_id"]
    spans = finished(telemetry)
    assert {f"{s.context.trace_id:032x}" for s in spans} == {reference["trace_id"]}
    calls = [s for s in spans if s.name == "llm.request"]
    assert len(calls) == 2 and len({s.context.span_id for s in calls}) == 2
    assert all(s.parent and s.attributes["purpose"] == "review" for s in calls)
    assert all(s.attributes["langfuse.observation.type"] == "span" for s in calls)
    assert all("gen_ai.usage.input_tokens" not in s.attributes for s in calls)
    rerun, _ = tracing.new_trace("incident-rerun")
    assert rerun["trace_id"] != reference["trace_id"]


def test_concurrent_investigations_do_not_share_trace_context(telemetry):
    async def operation(identity):
        with tracing.investigation(identity):
            before = tracing.current_reference()
            await asyncio.sleep(0)
            with tracing.span("retrieval"):
                assert tracing.current_reference() == before
            return before["trace_id"]
    async def scenario():
        return await asyncio.gather(operation("one"), operation("two"))
    assert len(set(asyncio.run(scenario()))) == 2


def test_metadata_only_and_safe_exception_records(telemetry):
    with (
        tracing.investigation("private"),
        pytest.raises(ValueError),
        tracing.model_request("private incident body", "private system", "alias") as observation,
    ):
        observation.content(output="private model output")
        raise ValueError("private incident body observability-test-secret")
    output = str([(dict(s.attributes), s.events, s.status.description) for s in finished(telemetry)])
    assert "private incident body" not in output
    assert "private system" not in output
    assert "private model output" not in output
    assert "observability-test-secret" not in output
    assert "ValueError" in output


def test_opt_in_content_is_redacted_before_export(telemetry):
    runtime, _ = telemetry
    runtime.settings.capture_content = True
    with (
        tracing.investigation("capture"),
        tracing.model_request("api_key=embedded-secret", "observability-test-secret", "alias") as observation,
    ):
        observation.content(output="https://user:password@example.com/path")
    output = str([dict(s.attributes) for s in finished(telemetry)])
    assert "embedded-secret" not in output
    assert "observability-test-secret" not in output
    assert "user:password" not in output
    assert "REDACTED" in output


def test_json_manifest_covers_exact_transmitted_text_and_item_ids():
    content = {"task": "same", "evidence": [{"id": "e1", "kind": "log", "data": "same"},
                                           {"id": "e2", "kind": "code_graph", "data": "same"}],
               "runbooks": [], "response_schema": {"type": "object"}}
    text = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    prompt = categorized(text, content, compact=True)
    assert isinstance(prompt, str) and prompt == text
    assert "".join(s.text for s in prompt.sections) == text
    assert [(s.evidence_id, s.category) for s in prompt.sections if s.evidence_id] == [("e1", "logs"), ("e2", "code_graph")]
    assert manifest(prompt, "system instructions")[0].category == "system"
    assert "omitted" not in str(prompt.sections)


def test_final_single_pass_manifest_tracks_clipped_rendering():
    from incident_copilot.graph.nodes.build_hypotheses import build_diagnosis_prompt
    state = {"parsed_log": {}, "candidates": [], "deep_analysis": {"huge.py": {"content": "x" * 10000000}},
             "runbooks": []}
    prompt = build_diagnosis_prompt(state)
    assert isinstance(prompt, Prompt)
    assert "x" * 100000 not in prompt
    assert "".join(s.text for s in prompt.sections) == prompt
    assert all("x" * 100000 not in s.text for s in prompt.sections)


def test_token_counter_failure_is_unavailable_and_never_counts_via_provider(telemetry, monkeypatch):
    from incident_copilot.observability.tokens import TokenCounter
    calls = []
    client_type = httpx.Client
    def handle(request):
        calls.append(request)
        return httpx.Response(503)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_type(transport=httpx.MockTransport(handle), **kwargs))
    counter = TokenCounter(telemetry[0])
    reference, carrier = tracing.new_trace("counter")
    counter._count([Section("system", "hello")], "alias", "http://gateway/v1", "key", carrier, {})
    counter.close()
    assert len(calls) == 1
    assert calls[0].url.path == "/utils/token_counter"
    assert calls[0].url.params["call_endpoint"] == "false"
    span = next(s for s in finished(telemetry) if s.name == "prompt.composition")
    assert json.loads(span.attributes["sections"])[0]["tokens_estimated"] is None
    assert f"{span.context.trace_id:032x}" == reference["trace_id"]


def test_token_counter_caches_original_content_and_bounds_queue(telemetry, monkeypatch):
    from incident_copilot.observability.tokens import TokenCounter
    requests = []
    client_type = httpx.Client
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"total_tokens": 7, "model_used": "real-model", "tokenizer_type": "local"})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_type(transport=httpx.MockTransport(handle), **kwargs))
    counter = TokenCounter(telemetry[0])
    _, carrier = tracing.new_trace("count-success")
    sections = [Section("logs", "api_key=actual-transmitted-secret")]
    for _ in range(2):
        counter._count(sections, "alias", "http://gateway/v1", "key", carrier, {})
    assert len(requests) == 1
    assert requests[0]["prompt"] == sections[0].text
    for _ in range(32):
        assert counter.slots.acquire(blocking=False)
    counter.submit(sections, "alias", "http://gateway/v1", "key", carrier, {})
    assert len(requests) == 1
    for _ in range(32):
        counter.slots.release()
    counter.close()
    records = [json.loads(s.attributes["sections"])[0] for s in finished(telemetry) if s.name == "prompt.composition"]
    assert all(record["tokens_estimated"] == 7 for record in records)


def test_gateway_adapter_propagates_parent_and_preserves_payload(telemetry, monkeypatch):
    from incident_copilot.llm.adapters.openai_compatible import OpenAICompatibleClient
    requests = []
    async_type = httpx.AsyncClient
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                        "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
                              headers={"x-litellm-call-id": "call-1"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: async_type(transport=httpx.MockTransport(handle), **kwargs))
    async def scenario():
        with tracing.investigation("adapter"):
            reference = tracing.current_reference()
            result = await OpenAICompatibleClient("http://gateway/v1", "gateway-secret", "alias").complete("question", "system")
            assert result == "ok"
            assert requests[0].headers["traceparent"].split("-")[1] == reference["trace_id"]
    asyncio.run(scenario())
    payload = json.loads(requests[0].content)
    assert payload == {"model": "alias", "messages": [{"role": "system", "content": "system"},
                                                    {"role": "user", "content": "question"}], "temperature": 0}
    calls = [s for s in finished(telemetry) if s.name == "llm.request"]
    assert len(calls) == 1 and calls[0].attributes["gateway_call_id"] == "call-1"
    assert calls[0].attributes["langfuse.observation.type"] == "span"


def test_direct_ollama_records_one_generation_with_reported_usage(telemetry, monkeypatch):
    from incident_copilot.llm.adapters.ollama import OllamaClient
    async_type = httpx.AsyncClient
    def handle(request):
        return httpx.Response(200, json={"response": "ok", "prompt_eval_count": 12, "eval_count": 4})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: async_type(transport=httpx.MockTransport(handle), **kwargs))
    async def scenario():
        with tracing.investigation("ollama"):
            assert await OllamaClient("http://ollama", "local-model").complete("prompt") == "ok"
    asyncio.run(scenario())
    generations = [s for s in finished(telemetry) if s.attributes.get("langfuse.observation.type") == "generation"]
    assert len(generations) == 1
    assert generations[0].attributes["gen_ai.usage.input_tokens"] == 12
    assert generations[0].attributes["gen_ai.usage.output_tokens"] == 4
    assert not any("cost" in key for key in generations[0].attributes)


def test_temporal_interceptor_uses_same_provider(telemetry):
    from temporalio.contrib.opentelemetry import TracingInterceptor
    interceptor = tracing.temporal_interceptors()[0]
    assert isinstance(interceptor, TracingInterceptor)
    assert interceptor.tracer is telemetry[0].tracer


def test_no_observability_credentials_in_analysis_settings():
    from incident_copilot.core.settings import Settings
    fields = Settings.model_fields
    assert not any("langfuse" in name for name in fields)


def test_exporter_removes_third_party_exception_events_and_status(telemetry, monkeypatch):
    from opentelemetry.exporter.otlp.proto.http import trace_exporter
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.trace import Status, StatusCode
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", lambda **kwargs: exporter)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(telemetry[0].original_exporter_factory(telemetry[0].settings, telemetry[0].clean)))
    with provider.get_tracer("third-party").start_as_current_span("activity") as span:
        span.record_exception(ValueError("raw incident body"))
        span.set_status(Status(StatusCode.ERROR, "raw incident body"))
        span.set_attribute("exception.message", "raw incident body")
        span.set_attribute("gen_ai.input.messages", "raw prompt body")
    safe = exporter.get_finished_spans()[0]
    assert not safe.events
    assert safe.status.description is None
    assert "raw" not in str(dict(safe.attributes))
    provider.shutdown()


@pytest.mark.skipif(__import__("os").getenv("IC_RUN_TEMPORAL_TESTS") != "1", reason="Requires local Temporal server")
def test_real_temporal_retry_redispatch_and_replay_keep_one_trace(telemetry, tmp_path, monkeypatch):
    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Replayer, Worker

    from incident_copilot.dashboard.activities import dashboard_stage_activity
    from incident_copilot.dashboard.models import IncidentSubmission, SettingsUpdate
    from incident_copilot.dashboard.service import DashboardService
    from incident_copilot.dashboard.store import ControlStore, utcnow
    from incident_copilot.graph.nodes import build_hypotheses
    from incident_copilot.workflows.temporal_workflows import DiagnoseIncidentWorkflow

    monkeypatch.setenv("IC_CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("IC_DATA_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IC_INDEX_DB_PATH", str(tmp_path / "indexes" / "repo.db"))
    model_calls = []

    class OfflineModel:
        async def complete(self, **kwargs):
            with tracing.model_request(kwargs["prompt"], kwargs["system_prompt"], "test-model"):
                model_calls.append(tracing.current_reference()["trace_id"])
                raise ConnectionError("provider unavailable")

    monkeypatch.setattr(build_hypotheses, "get_client", lambda *args, **kwargs: OfflineModel())

    @activity.defn(name="dashboard_stage_activity")
    async def retrying_stage(incident_id: str, stage: str):
        if stage == "correlating" and activity.info().attempt == 1:
            raise RuntimeError("retry fixture")
        return await dashboard_stage_activity(incident_id, stage)

    async def scenario():
        download_dir = tmp_path / "temporal"
        download_dir.mkdir()
        async with await WorkflowEnvironment.start_local(dev_server_log_level="error", download_dest_dir=str(download_dir)) as environment:
            monkeypatch.setenv("IC_TEMPORAL_HOST", environment.client.service_client.config.target_host)
            service = DashboardService(ControlStore(tmp_path / "control.db"))
            service.configuration.save(SettingsUpdate(values={"guardrails_mode": "off", "code_graph_enabled": False}))
            root = tmp_path / "source"
            root.mkdir()
            (root / "main.py").write_text("def charge():\n    raise ValueError('charge failed')\n")
            service.store.put("repositories", "repo", {"id": "repo", "name": "Test", "repo_root": str(root),
                "compose_path": None, "archived": False, "created_at": utcnow()})
            client = await service.gateway.client()
            async with Worker(client, task_queue=service.configuration.effective().temporal_task_queue,
                              workflows=[DiagnoseIncidentWorkflow], activities=[retrying_stage]):
                run = service.submit(IncidentSubmission(repository_id="repo", log_text='api | ValueError: charge failed\nFile "/app/main.py", line 2, in charge'))
                reference = run.observability
                await service.reconcile()
                service.store.update("incidents", run.id, {"dispatched": False})
                restarted = DashboardService(service.store)
                restarted.gateway.clients = service.gateway.clients
                await restarted.reconcile()
                handle = client.get_workflow_handle(f"incident-{run.id}")
                await asyncio.wait_for(handle.result(), timeout=45)
                await restarted.reconcile()
                saved = service.store.get("incidents", run.id)
                assert saved["status"] == "completed"
                assert saved["observability"]["trace_id"] == reference.trace_id
                assert saved["report"]["observability"]["trace_id"] == reference.trace_id
                assert any(row["attempt"] == 2 for row in saved["timeline"])
                history = await handle.fetch_history()
                assert "observability-test-secret" not in str(history.events)
                before = len(model_calls)
                await Replayer(workflows=[DiagnoseIncidentWorkflow], interceptors=tracing.temporal_interceptors()).replay_workflow(history)
                assert len(model_calls) == before == 1
                assert model_calls == [reference.trace_id]
                activities = [s for s in finished(telemetry) if s.name.startswith("RunActivity:")]
                assert len(activities) == 5
                assert {f"{s.context.trace_id:032x}" for s in activities} == {reference.trace_id}
    asyncio.run(scenario())


def test_iterative_manifest_excludes_clipped_evidence_items():
    from incident_copilot.graph.investigation import build_investigation_prompt
    state = {"reasoning": {"mode": "baseline"}, "evidence_records": {
        str(index): {"id": str(index), "kind": "source", "content": str(index) * 8000}
        for index in range(12)}, "runbooks": []}
    prompt = build_investigation_prompt(state, {"rounds": 0, "model_calls": 0, "tool_calls": 0})
    payload = json.loads(prompt)
    assert "".join(part.text for part in prompt.sections) == prompt
    assert {part.evidence_id for part in prompt.sections if part.evidence_id} == {item["id"] for item in payload["evidence"]}
    assert len(payload["evidence"]) < len(state["evidence_records"])


def test_review_and_guardrail_manifests_preserve_original_rendering():
    from incident_copilot.llm.reasoning import review_payload
    payload = review_payload({"hypotheses": []}, [{"id": "original", "kind": "log", "content": "original observation"}], [])
    text = json.dumps(payload, ensure_ascii=False)
    prompt = categorized(text, payload)
    assert prompt == text
    assert "".join(part.text for part in prompt.sections) == text
    assert any(part.evidence_id == "original" for part in prompt.sections)
    guarded = Prompt("raw classifier input", [Section("evidence", "raw classifier input")])
    assert "".join(part.text for part in manifest(guarded, "classifier system")) == "classifier systemraw classifier input"


def test_failed_export_does_not_change_analysis_result(telemetry, monkeypatch):
    from opentelemetry.sdk.trace.export import SpanExportResult
    runtime, exporter = telemetry
    monkeypatch.setattr(exporter, "export", lambda spans: SpanExportResult.FAILURE)
    with tracing.investigation("export-offline"), tracing.span("analysis.step"):
        result = "diagnosis still completes"
    runtime.provider.force_flush()
    assert result == "diagnosis still completes"


def test_template_like_untrusted_evidence_cannot_confuse_section_boundaries():
    from incident_copilot.graph.nodes.build_hypotheses import build_diagnosis_prompt
    state = {"parsed_log": {"error_lines": ["Top candidates: [123] Deep analysis: {}"]},
             "candidates": [], "deep_analysis": {}, "runbooks": []}
    prompt = build_diagnosis_prompt(state)
    assert "".join(part.text for part in prompt.sections) == prompt
    logs = [part.text for part in prompt.sections if part.category == "logs"]
    assert any("Top candidates:" in text for text in logs)


def test_export_redaction_preserves_json_structure_and_cleans_sequences(telemetry):
    runtime, _ = telemetry
    original = {"nested": {"api_key": "previously-unknown-key"}, "text": "observability-test-secret"}
    cleaned = json.loads(runtime.clean(json.dumps(original)))
    assert cleaned["nested"]["api_key"] == "[REDACTED]"
    assert "observability-test-secret" not in cleaned["text"]
    assert runtime.clean(("observability-test-secret", "safe")) == ("[REDACTED]", "safe")
