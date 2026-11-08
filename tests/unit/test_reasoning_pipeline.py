"""Evidence grounding, bounded review and durable recovery regressions."""

import asyncio
import copy
import json
import time
from dataclasses import replace

import pytest

from incident_copilot.core.settings import Settings
from incident_copilot.graph import investigation
from incident_copilot.graph.diagnosis_graph import run_diagnosis_graph
from incident_copilot.graph.nodes import build_hypotheses
from incident_copilot.llm import reasoning
from incident_copilot.llm.guardrails import GuardrailBlocked
from incident_copilot.reporting.markdown_renderer import render_markdown
from incident_copilot.reporting.report_builder import build_report
from tests.action_plan_fixtures import action_plan
from tests.unit import test_investigation_recovery as recovery
from tests.unit.test_llm_diagnosis import OUTCOME, hypothesis, state

execution = recovery.execution


def draft():
    return {
        **OUTCOME,
        "review_reasons": [],
        "hypotheses": [
            hypothesis(1, title="Invalid payment"),
            hypothesis(2, title="Invalid configuration"),
        ],
    }


def revision(value, original_rank=2):
    h = copy.deepcopy(value["hypotheses"][original_rank - 1])
    h.update(rank=1, original_rank=original_rank)
    return {**OUTCOME, "review_reasons": [], "hypotheses": [h]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,calls", [("baseline", 1), ("structured", 1), ("structured_review", 2)]
)
async def test_single_pass_modes_keep_localization_and_review_reranks(monkeypatch, mode, calls):
    prompts = []
    value = draft()

    class Model:
        async def complete(self, **kwargs):
            prompts.append(kwargs)
            return json.dumps(value if len(prompts) == 1 else revision(value))

    monkeypatch.setattr(build_hypotheses, "get_client", lambda *a, **k: Model())
    result = await run_diagnosis_graph(
        state(), Settings(_env_file=None, guardrails_mode="off", llm_reasoning_mode=mode)
    )
    report = build_report(result)
    assert len(prompts) == calls == report.reasoning.model_calls
    assert report.engine == "deterministic+llm"
    assert report.candidates[0].score == 20
    assert report.reasoning.mode == mode
    assert ("Assess evidence, causal mechanism" in prompts[0]["system_prompt"]) == (
        mode != "baseline"
    )
    if mode == "structured_review":
        assert report.hypotheses[0].title == "Invalid configuration"
        assert report.reasoning.review_status == "completed"
        assert result["reasoning_draft"]["hypotheses"][0]["title"] == "Invalid payment"
    assert all(
        h.confidence is None and h.confidence_status == "unavailable" for h in report.hypotheses
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "invalid_json",
        "new_cause",
        "citation",
        "policy",
        "timeout",
        "dropped_contradiction",
        "dropped_gap",
        "runbook",
    ],
)
async def test_review_failure_retains_valid_draft_and_safe_diagnostics(monkeypatch, failure):
    value = draft()
    if failure == "dropped_contradiction":
        value["hypotheses"][1]["contradicting_evidence_refs"] = value["hypotheses"][1][
            "action_plan"
        ]["supporting_evidence"][0]["evidence_refs"]
    if failure == "dropped_gap":
        value["hypotheses"][1]["action_plan"]["missing_evidence"] = [
            "Runtime input remains unknown"
        ]
    calls = 0

    class Model:
        async def complete(self, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return json.dumps(value)
            if failure == "invalid_json":
                return "private-provider-sentinel"
            if failure == "policy":
                raise GuardrailBlocked("guardrail_output_blocked")
            if failure == "timeout":
                await asyncio.Event().wait()
            changed = revision(value)
            if failure == "new_cause":
                changed["hypotheses"][0]["title"] = "Invented cause"
            if failure == "citation":
                changed["hypotheses"][0]["evidence_refs"] = ["not-collected"]
            if failure == "dropped_contradiction":
                changed["hypotheses"][0]["contradicting_evidence_refs"] = []
            if failure == "dropped_gap":
                changed["hypotheses"][0]["action_plan"]["missing_evidence"] = []
            if failure == "runbook":
                changed["hypotheses"][0]["action_plan"]["runbooks"] = [
                    {"runbook_id": "invented", "section": "unknown", "reason": "unsupported"}
                ]
            return json.dumps(changed)

    monkeypatch.setattr(reasoning, "REVIEW_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(build_hypotheses, "get_client", lambda *a, **k: Model())
    result = await run_diagnosis_graph(
        state(),
        Settings(_env_file=None, guardrails_mode="off", llm_reasoning_mode="structured_review"),
    )
    report = build_report(result)
    assert report.hypotheses[0].title == "Invalid payment"
    assert report.reasoning.review_status == "failed"
    assert report.reasoning.model_calls == 2
    assert report.fallback_reason is None
    assert "private-provider-sentinel" not in report.model_dump_json()
    if failure in {
        "invalid_json",
        "new_cause",
        "citation",
        "dropped_contradiction",
        "dropped_gap",
        "runbook",
    }:
        assert report.validation_errors[0].stage == "diagnosis_review"


@pytest.mark.asyncio
async def test_unambiguous_diagnosis_has_no_review(monkeypatch):
    value = draft()
    value["hypotheses"] = value["hypotheses"][:1]

    class Model:
        async def complete(self, **kwargs):
            return json.dumps(value)

    monkeypatch.setattr(build_hypotheses, "get_client", lambda *a, **k: Model())
    result = await run_diagnosis_graph(
        state(),
        Settings(_env_file=None, guardrails_mode="off", llm_reasoning_mode="structured_review"),
    )
    assert result["reasoning"]["model_calls"] == 1
    assert result["reasoning"]["review_status"] == "not_needed"


@pytest.mark.asyncio
async def test_contradictions_survive_review_and_exports(monkeypatch):
    value = draft()
    ref = value["hypotheses"][0]["action_plan"]["supporting_evidence"][0]["evidence_refs"][0]
    value["hypotheses"][1]["contradicting_evidence_refs"] = [ref]

    class Model:
        calls = 0

        async def complete(self, **kwargs):
            self.calls += 1
            return json.dumps(value if self.calls == 1 else revision(value))

    model = Model()
    monkeypatch.setattr(build_hypotheses, "get_client", lambda *a, **k: model)
    report = build_report(
        await run_diagnosis_graph(
            state(),
            Settings(_env_file=None, guardrails_mode="off", llm_reasoning_mode="structured_review"),
        )
    )
    assert report.hypotheses[0].contradicting_evidence_refs == [ref]
    assert "Contrary evidence" in render_markdown(report)
    assert "structured-review-v1" in render_markdown(report)


@pytest.mark.asyncio
async def test_review_limits_and_cancellation_do_not_consume_extra_calls(monkeypatch):
    value = draft()
    calls = 0

    class Model:
        async def complete(self, **kwargs):
            nonlocal calls
            calls += 1
            raise asyncio.CancelledError

    async def run(deadline, reserve, evidence):
        return await reasoning.review_diagnosis(
            value,
            evidence,
            [],
            summary=reasoning.reasoning_summary("structured_review", 1),
            client=Model(),
            deadline=deadline,
            reserve=reserve,
        )

    result = await run(time.time() - 1, lambda: True, [])
    assert result["reasoning"]["review_failure"] == "deadline"
    result = await run(time.time() + 10, lambda: False, [])
    assert result["reasoning"]["review_failure"] == "model_call_limit"
    result = await run(
        time.time() + 10,
        lambda: True,
        [
            {
                "id": value["hypotheses"][0]["action_plan"]["supporting_evidence"][0][
                    "evidence_refs"
                ][0],
                "content": "x" * 40000,
            }
        ],
    )
    assert result["reasoning"]["review_failure"] == "prompt_limit"
    assert calls == 0
    with pytest.raises(asyncio.CancelledError):
        await run(time.time() + 10, lambda: True, [])
    assert calls == 1


def investigation_decision(prompt, finish=True):
    payload = json.loads(prompt)
    ref = payload["evidence"][0]["id"]
    return {
        **OUTCOME,
        "review_reasons": ["causal_gap"],
        "action": "finish" if finish else "retrieve",
        "hypotheses": [
            {
                **hypothesis(1),
                "id": "cause-1",
                "evidence_refs": [ref],
                "contradicting_evidence_refs": [],
                "action_plan": action_plan([ref]),
            }
        ],
        "unresolved_gaps": ["Runtime input is unavailable"],
        "requests": []
        if finish
        else [
            {
                "tool": "read_source",
                "hypothesis_id": "cause-1",
                "question": "Does the configuration support or weaken this mechanism?",
                "file_path": "cache.py",
                "line_start": 1,
                "line_end": 4,
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt_during_review", [False, True])
async def test_iterative_review_recovers_without_repeating_or_changing_mode(
    execution, monkeypatch, interrupt_during_review
):
    settings, runtime, payload, _, journal = execution
    settings = settings.model_copy(
        update={"llm_reasoning_mode": "structured_review", "guardrails_mode": "off"}
    )
    entered = asyncio.Event()
    calls = []

    class Model:
        async def complete(self, **kwargs):
            calls.append(kwargs)
            if kwargs["system_prompt"] == reasoning.REVIEW_SYSTEM_PROMPT:
                if interrupt_during_review:
                    entered.set()
                    await asyncio.Event().wait()
                original = json.loads(kwargs["prompt"])["draft"]
                return json.dumps(
                    {
                        **OUTCOME,
                        "review_reasons": [],
                        "hypotheses": [
                            {k: v for k, v in original["hypotheses"][0].items() if k != "id"}
                            | {"original_rank": 1}
                        ],
                    }
                )
            return json.dumps(investigation_decision(kwargs["prompt"]))

    monkeypatch.setattr(investigation, "get_client", lambda *a, **k: Model())

    async def progress(update):
        if (
            not interrupt_during_review
            and (update.get("reasoning") or {}).get("review_status") == "completed"
        ):
            entered.set()
            await asyncio.Event().wait()

    runtime = replace(runtime, progress=progress)
    task = asyncio.create_task(run_diagnosis_graph(payload, settings, runtime))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    runtime = replace(runtime, progress=None)
    result = await run_diagnosis_graph(
        payload, settings.model_copy(update={"llm_reasoning_mode": "baseline"}), runtime
    )
    assert len(calls) == result["investigation"]["model_calls"] == 2
    assert result["reasoning"]["mode"] == "structured_review"
    assert result["reasoning"]["review_status"] == (
        "failed" if interrupt_during_review else "completed"
    )
    assert journal().get("terminal_review") is not None
    assert journal().budget()["model_calls"] == 2


@pytest.mark.asyncio
async def test_iterative_retrieval_has_priority_over_review_at_four_calls(execution, monkeypatch):
    settings, runtime, payload, _, _ = execution
    settings = settings.model_copy(
        update={"llm_reasoning_mode": "structured_review", "guardrails_mode": "off"}
    )

    class Model:
        calls = 0

        async def complete(self, **kwargs):
            self.calls += 1
            value = investigation_decision(kwargs["prompt"], finish=self.calls == 4)
            if self.calls < 4:
                value["requests"][0].update(line_start=self.calls, line_end=self.calls)
            return json.dumps(value)

    model = Model()
    monkeypatch.setattr(investigation, "get_client", lambda *a, **k: model)
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert model.calls == result["investigation"]["model_calls"] == 4
    assert result["investigation"]["rounds"] == 3
    assert result["reasoning"]["review_status"] == "skipped"
    assert result["reasoning"]["review_failure"] == "model_call_limit"


@pytest.mark.asyncio
async def test_insufficient_evidence_review_cannot_invent_certainty(monkeypatch):
    value = draft()
    value.update(diagnosis_status="insufficient_evidence", review_reasons=["causal_gap"])

    class Model:
        async def complete(self, **kwargs):
            return json.dumps(revision(value))

    result = await reasoning.review_diagnosis(
        value,
        [],
        [],
        summary=reasoning.reasoning_summary("structured_review", 1),
        client=Model(),
        deadline=time.time() + 10,
        reserve=lambda: True,
    )
    assert result["decision"]["diagnosis_status"] == "insufficient_evidence"
    assert result["validation_errors"][0]["issues"][0]["code"] == "review_status_upgrade"


@pytest.mark.asyncio
async def test_structured_retrieval_requires_a_hypothesis_or_explicit_gap(execution, monkeypatch):
    settings, runtime, payload, _, _ = execution
    settings = settings.model_copy(
        update={"llm_reasoning_mode": "structured", "guardrails_mode": "off"}
    )

    class Model:
        async def complete(self, **kwargs):
            value = investigation_decision(kwargs["prompt"], finish=False)
            value["requests"][0]["hypothesis_id"] = "not-a-hypothesis"
            return json.dumps(value)

    monkeypatch.setattr(investigation, "get_client", lambda *a, **k: Model())
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert result["investigation"]["tool_calls"] == 0
    assert result["validation_errors"][0]["issues"][0]["code"] == "hypothesis_request_mismatch"


def test_reasoning_settings_are_versioned_without_restart(tmp_path):
    from incident_copilot.dashboard.config import Configuration
    from incident_copilot.dashboard.models import SettingsUpdate
    from incident_copilot.dashboard.store import ControlStore

    config = Configuration(ControlStore(tmp_path / "control.db"))
    for mode in ("baseline", "structured", "structured_review"):
        view = config.save(SettingsUpdate(values={"llm_reasoning_mode": mode}))
        assert view.active["llm_reasoning_mode"] == mode
        assert "llm_reasoning_mode" not in view.restart_required
        revision = config.revision()
        assert config.store.get("revisions", revision)["llm_reasoning_mode"] == mode
    with pytest.raises(ValueError):
        config.save(SettingsUpdate(values={"llm_reasoning_mode": "invalid"}))


@pytest.mark.asyncio
async def test_unavailable_pinned_prompt_version_stops_without_new_model_calls(execution):
    from tests.unit.test_investigation_recovery import interrupt_after_retrieval

    settings, runtime, payload, model, journal = execution
    settings = settings.model_copy(update={"guardrails_mode": "off"})
    await interrupt_after_retrieval(payload, settings, runtime)
    count = len(model.prompts)
    pinned = journal().get("reasoning_config")
    pinned["prompt_version"] = "unavailable-version"
    journal().put("reasoning_config", pinned)
    result = await run_diagnosis_graph(payload, settings, runtime)
    assert len(model.prompts) == count
    assert result["reasoning"]["prompt_version"] == "unavailable-version"
    assert result["investigation"]["stop_reason"] == "reasoning_version_unavailable"
