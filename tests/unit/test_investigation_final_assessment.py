"""Terminal retrieval feedback is assessed within durable execution limits."""
import asyncio
import json
import time
from dataclasses import replace

import pytest

from incident_copilot.graph import investigation
from incident_copilot.graph.diagnosis_graph import run_diagnosis_graph
from incident_copilot.reporting.markdown_renderer import render_markdown
from incident_copilot.reporting.report_builder import build_report
from incident_copilot.schemas.investigation import EvidenceItem, ToolOutcome
from tests.unit import test_investigation_recovery as recovery
from tests.unit.test_investigation_limits import decision, read

execution = recovery.execution


def abstention(reason):
    return json.dumps({
        "action": "finish", "requests": [], "hypotheses": [],
        "diagnosis_status": "insufficient_evidence", "diagnosis_reason": reason,
        "next_checks": ["Collect the missing runtime observation"], "unresolved_gaps": [reason],
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome_kind", ["empty", "unavailable", "duplicate"])
async def test_no_new_records_still_reaches_final_assessment(execution, monkeypatch, outcome_kind):
    settings, runtime, payload, _, _ = execution
    prompts = []
    search = {"tool": "search_source", "question": "Does this snapshot reference the pool?",
              "query": "absent-pool-name", "service": "cache"}

    class Model:
        async def complete(self, **kwargs):
            prompt = json.loads(kwargs["prompt"])
            prompts.append(prompt)
            if len(prompts) == 1:
                return decision(kwargs["prompt"], [search])
            assert prompt["assessment_mode"] == "final"
            assert prompt["retrieval_stop_reason"] == "no_new_evidence"
            assert kwargs["response_schema"] == prompt["response_schema"]
            assert kwargs["response_schema"]["properties"]["action"]["enum"] == ["finish"]
            assert kwargs["response_schema"]["properties"]["requests"]["maxItems"] == 0
            outcome, = prompt["completed_requests"]
            assert outcome["question"] == search["question"]
            assert outcome["scope"]["query"] == search["query"]
            assert outcome["scope"]["service"] == "cache"
            assert outcome["new_evidence_count"] == 0
            if outcome_kind == "empty":
                assert outcome["status"] == "empty"
            elif outcome_kind == "unavailable":
                assert outcome["status"] == "unavailable" and outcome["reason"] == "source_unavailable"
            else:
                assert outcome["status"] == "ok" and outcome["evidence_ids"]
            return abstention("The lookup did not establish pool exhaustion; runtime evidence is missing.")

    async def execute(self, request, request_id):
        if outcome_kind == "unavailable":
            return ToolOutcome(request_id=request_id, tool=request.tool,
                               status="unavailable", reason="source_unavailable")
        # Return an existing observation so record count does not increase.
        existing = EvidenceItem.model_validate(prompts[0]["evidence"][0])
        return ToolOutcome(request_id=request_id, tool=request.tool, status="ok", evidence=[existing])

    monkeypatch.setattr(investigation, "get_client", lambda *a, **k: Model())
    if outcome_kind != "empty":
        monkeypatch.setattr(investigation.EvidenceTools, "execute", execute)
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert len(prompts) == result["investigation"]["model_calls"] == 2
    assert result["investigation"]["tool_calls"] == 1
    assert result["investigation"]["stop_reason"] == "no_new_evidence"
    assert result["investigation"]["unassessed_request_ids"] == []
    assert result["diagnosis_status"] == "insufficient_evidence" and result["hypotheses"] == []


@pytest.mark.asyncio
async def test_unavailable_runtime_telemetry_is_an_explicit_gap(execution, monkeypatch):
    settings, runtime, payload, _, _ = execution
    calls = []

    class RuntimeClient:
        def scope(self):
            return {"services": ["cache"], "trace_ids": []}

        async def execute(self, request, request_id):
            calls.append(request)
            return ToolOutcome(request_id=request_id, tool=request.tool,
                               status="unavailable", reason="collector_offline")

    runtime = replace(runtime, runtime_client=RuntimeClient())
    payload["runtime_scope"] = runtime.runtime_client.scope()

    class Model:
        async def complete(self, **kwargs):
            prompt = json.loads(kwargs["prompt"])
            if not prompt["completed_requests"]:
                return decision(kwargs["prompt"], [{
                    "tool": "get_runtime_evidence", "question": "Was cache resource pressure observed?",
                    "capability": "resources", "service": "cache", "lookback_minutes": 5,
                }])
            outcome, = prompt["completed_requests"]
            assert outcome["reason"] == "collector_offline"
            assert outcome["scope"]["capability"] == "resources"
            assert outcome["scope"]["lookback_minutes"] == 5
            return abstention("Resource pressure is unknown because the collector is offline.")

    monkeypatch.setattr(investigation, "get_client", lambda *a, **k: Model())
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert len(calls) == 1
    assert "collector is offline" in result["diagnosis_reason"]
    assert result["investigation"]["unassessed_request_ids"] == []


@pytest.mark.asyncio
async def test_content_limit_assesses_retained_contrary_evidence(execution, monkeypatch):
    settings, runtime, payload, _, _ = execution
    monkeypatch.setattr(investigation, "MAX_EVIDENCE_BYTES", 1800)
    prompts = []

    class Model:
        async def complete(self, **kwargs):
            prompt = json.loads(kwargs["prompt"])
            prompts.append(prompt)
            if len(prompts) == 1:
                return decision(kwargs["prompt"], [read(1)])
            assert prompt["retrieval_stop_reason"] == "evidence_limit"
            outcome, = prompt["completed_requests"]
            assert outcome["truncated"] and outcome["omitted_evidence_count"] == 1
            assert outcome["limitations"] == ["response_clipped", "evidence_limit"]
            assert outcome["evidence_ids"] == ["contrary"]
            assert any(item["id"] == "contrary" for item in prompt["evidence"])
            answer = json.loads(decision(kwargs["prompt"]))
            answer["hypotheses"][0].update(
                title="Cache explanation weakened", contradicting_evidence_refs=["contrary"],
            )
            return json.dumps(answer)

    async def execute(self, request, request_id):
        return ToolOutcome(request_id=request_id, tool=request.tool, status="ok", evidence=[
            EvidenceItem(id="contrary", kind="source", repository=str(runtime.repo_root),
                         revision="a" * 40, content="Pool pressure is not observed here.",
                         limitations=["response_clipped"], truncated=True, request_id=request_id),
            EvidenceItem(id="too-large", kind="source", repository=str(runtime.repo_root),
                         revision="a" * 40, content="x" * 3000, request_id=request_id),
        ])

    monkeypatch.setattr(investigation, "get_client", lambda *a, **k: Model())
    monkeypatch.setattr(investigation.EvidenceTools, "execute", execute)
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert result["hypotheses"][0]["title"] == "Cache explanation weakened"
    assert result["investigation"]["model_calls"] == 2
    assert result["investigation"]["unassessed_request_ids"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["retrieve_again", "invalid_json", "provider_failure"])
async def test_failed_final_assessment_keeps_draft_and_reports_unassessed_outcome(
    execution, monkeypatch, failure,
):
    settings, runtime, payload, _, _ = execution
    calls = 0

    class Model:
        async def complete(self, **kwargs):
            nonlocal calls
            calls += 1
            request = {"tool": "search_source", "question": "Find the missing setting", "query": "absent"}
            if calls == 1 or failure == "retrieve_again":
                return decision(kwargs["prompt"], [request])
            if failure == "provider_failure":
                raise ConnectionError("private-provider-error")
            return "invalid-json-private-output"

    monkeypatch.setattr(investigation, "get_client", lambda *a, **k: Model())
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert calls == 2 and result["investigation"]["tool_calls"] == 1
    assert result["hypotheses"][0]["title"] == "Investigate cache configuration"
    assert len(result["investigation"]["unassessed_request_ids"]) == 1
    assert "not incorporated" in render_markdown(build_report(result))
    assert "private-" not in json.dumps(result)
    if failure == "retrieve_again":
        assert result["validation_errors"][0]["issues"][0]["code"] == "final_assessment_requires_finish"


@pytest.mark.asyncio
async def test_final_call_cannot_launch_unassessable_retrieval(execution, monkeypatch):
    settings, runtime, payload, _, _ = execution
    monkeypatch.setattr(investigation, "MAX_MODEL_CALLS", 2)
    calls = 0

    class Model:
        async def complete(self, **kwargs):
            nonlocal calls
            calls += 1
            return decision(kwargs["prompt"], [read(calls)])

    monkeypatch.setattr(investigation, "get_client", lambda *a, **k: Model())
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert calls == result["investigation"]["model_calls"] == 2
    assert result["investigation"]["tool_calls"] == 1
    assert result["validation_errors"][0]["issues"][0]["code"] == "final_assessment_requires_finish"
    assert len(result["investigation"]["unassessed_request_ids"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("exhaustion", ["deadline", "model_calls"])
async def test_retry_cannot_reset_limits_to_assess_terminal_feedback(execution, monkeypatch, exhaustion):
    settings, runtime, payload, _, journal = execution
    calls = 0

    class Model:
        async def complete(self, **kwargs):
            nonlocal calls
            calls += 1
            return decision(kwargs["prompt"], [{
                "tool": "search_source", "question": "Find the missing setting", "query": "absent",
            }])

    monkeypatch.setattr(investigation, "get_client", lambda *a, **k: Model())
    await recovery.interrupt_after_retrieval(payload, settings, runtime)
    budget = journal().budget()
    if exhaustion == "deadline":
        budget["deadline"] = time.time() - 1
    else:
        budget["model_calls"] = investigation.MAX_MODEL_CALLS
    journal().put("budget", budget)
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert calls == 1
    assert result["investigation"]["stop_reason"] == (
        "deadline" if exhaustion == "deadline" else "model_call_limit"
    )
    assert len(result["investigation"]["unassessed_request_ids"]) == 1
    assert result["investigation"]["incomplete"]
    assert "not incorporated" in render_markdown(build_report(result))


@pytest.mark.asyncio
async def test_saved_final_assessment_is_reused_after_crash_and_deadline(execution, monkeypatch):
    settings, runtime, payload, _, journal = execution
    calls = 0
    original_put = investigation.InvestigationStore.put

    class WorkerCrash(BaseException):
        pass

    class Model:
        async def complete(self, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return decision(kwargs["prompt"], [{
                    "tool": "search_source", "question": "Find the missing setting", "query": "absent",
                }])
            return abstention("The lookup could not verify the proposed cause.")

    def crash_after_save(self, key, value):
        original_put(self, key, value)
        if key.startswith("assessment:") and value.get("final_reason"):
            raise WorkerCrash()

    monkeypatch.setattr(investigation, "get_client", lambda *a, **k: Model())
    monkeypatch.setattr(investigation.InvestigationStore, "put", crash_after_save)
    with pytest.raises(WorkerCrash):
        await run_diagnosis_graph(payload, settings, runtime)
    monkeypatch.setattr(investigation.InvestigationStore, "put", original_put)
    budget = journal().budget()
    budget["deadline"] = time.time() - 1
    journal().put("budget", budget)
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert calls == result["investigation"]["model_calls"] == 2
    assert result["investigation"]["tool_calls"] == 1
    assert result["diagnosis_status"] == "insufficient_evidence"
    assert result["investigation"]["unassessed_request_ids"] == []
    assert await run_diagnosis_graph(payload, settings, runtime) == result
    assert calls == 2


@pytest.mark.asyncio
async def test_legacy_saved_assessment_is_reused_without_resetting_deadline(execution, monkeypatch):
    settings, runtime, payload, model, journal = execution
    original_put = investigation.InvestigationStore.put

    class WorkerCrash(BaseException):
        pass

    def save_legacy_result(self, key, value):
        if key.startswith("assessment:") and "decision" in value:
            prompt = model.prompts[-1]
            legacy_key = "assessment:" + investigation.digest([
                self.budget()["rounds"], [item["id"] for item in prompt["evidence"]],
            ])
            original_put(self, legacy_key, {k: value[k] for k in ("decision", "duration_ms")})
            raise WorkerCrash()
        original_put(self, key, value)

    monkeypatch.setattr(investigation.InvestigationStore, "put", save_legacy_result)
    with pytest.raises(WorkerCrash):
        await run_diagnosis_graph(payload, settings, runtime)
    monkeypatch.setattr(investigation.InvestigationStore, "put", original_put)
    budget = journal().budget()
    budget["deadline"] = time.time() - 1
    journal().put("budget", budget)
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert len(model.prompts) == result["investigation"]["model_calls"] == 1
    assert result["engine"] == "deterministic+llm"
    assert result["hypotheses"][0]["title"] == "Cache path needs inspection"
    assert result["investigation"]["stop_reason"] == "deadline"


@pytest.mark.asyncio
async def test_cancellation_during_final_assessment_preserves_budget(execution, monkeypatch):
    settings, runtime, payload, _, journal = execution
    entered = asyncio.Event()
    calls = 0
    interrupted = False

    class Model:
        async def complete(self, **kwargs):
            nonlocal calls, interrupted
            calls += 1
            if calls == 1:
                return decision(kwargs["prompt"], [{
                    "tool": "search_source", "question": "Find the missing setting", "query": "absent",
                }])
            if not interrupted:
                interrupted = True
                entered.set()
                await asyncio.Event().wait()
            return abstention("The lookup did not verify the cause.")

    monkeypatch.setattr(investigation, "get_client", lambda *a, **k: Model())
    task = asyncio.create_task(run_diagnosis_graph(payload, settings, runtime))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert journal().budget()["model_calls"] == 2
    assert journal().budget()["tool_calls"] == 1
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert result["investigation"]["model_calls"] == calls == 3
    assert result["investigation"]["tool_calls"] == 1
    assert result["investigation"]["unassessed_request_ids"] == []
