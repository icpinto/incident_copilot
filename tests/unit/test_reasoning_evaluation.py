"""Promotion requires matched, adjudicated held-out evidence and measured latency."""

import copy

import pytest

from incident_copilot.evaluation.diagnosis_review import IncidentReview, evaluate_reviews
from incident_copilot.evaluation.reasoning_gates import compare_policies


def dataset(mode, correct_count):
    records = []
    for index in range(50):
        sufficient = index < 35
        records.append(
            IncidentReview(
                incident_id=str(index),
                repository_revision="abc",
                model_version="llama-pinned",
                prompt_version=mode,
                diagnosis_status="provisional" if sufficient else "insufficient_evidence",
                evidence_sufficient=sufficient,
                dataset_split="held_out",
                case_origin="verified",
                independently_adjudicated=True,
                inference_mode="single_pass",
                reasoning_mode=mode,
                evidence_fingerprint="snapshot",
                latency_seconds=10 if mode == "baseline" else 12,
                unsupported_claims=0,
                claim_count=1 if sufficient else 0,
                hypotheses=[
                    {
                        "hypothesis_id": "h",
                        "rank": 1,
                        "model_score": 0.8,
                        "causal_correct": index < correct_count,
                    }
                ]
                if sufficient
                else [],
            )
        )
    return records


def test_causal_success_counts_abstention_as_miss_and_records_review_damage():
    records = dataset("structured", 30)
    records[0].diagnosis_status = "insufficient_evidence"
    records[0].hypotheses = []
    records[0].draft_causal_correct = True
    records[31].hypotheses[0].causal_correct = True
    records[31].draft_causal_correct = False
    metrics = evaluate_reviews(records)
    assert metrics["causal_diagnosis_success"]["denominator"] == 35
    assert metrics["causal_diagnosis_success"]["numerator"] == 30
    assert metrics["review_effect"]["improved_drafts"] == 1
    assert metrics["review_effect"]["damaged_drafts"] == 1
    assert metrics["causal_diagnosis_success"]["wilson_95_interval"] is not None


def test_gates_recommend_simplest_passing_mode_without_changing_settings():
    result = compare_policies(dataset("baseline", 20), dataset("structured", 25))
    assert result["recommended_mode"] == "structured"
    assert result["structured"]["passes"]
    assert result["structured"]["paired_bootstrap_95_interval"] is not None
    assert result["settings_changed"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        "synthetic",
        "development",
        "unmatched",
        "model",
        "evidence",
        "latency",
        "claims",
        "unknown",
        "mixed_mode",
        "no_gain",
    ],
)
def test_incomplete_or_incomparable_evaluation_never_promotes(mutation):
    baseline, candidate = dataset("baseline", 20), dataset("structured", 25)
    if mutation == "synthetic":
        candidate[0].case_origin = "synthetic"
    elif mutation == "development":
        candidate[0].dataset_split = "development"
    elif mutation == "unmatched":
        candidate.pop()
    elif mutation == "model":
        candidate[0].model_version = "changed"
    elif mutation == "evidence":
        candidate[0].evidence_fingerprint = "changed"
    elif mutation == "latency":
        candidate[0].latency_seconds = None
    elif mutation == "claims":
        candidate[0].unsupported_claims = None
    elif mutation == "unknown":
        candidate[0].hypotheses[0].causal_correct = None
    elif mutation == "mixed_mode":
        candidate[0].inference_mode = "iterative"
    elif mutation == "no_gain":
        candidate = dataset("structured", 20)
    result = compare_policies(baseline, candidate)
    assert result["recommended_mode"] == "baseline"
    assert not result["structured"]["passes"]


def test_review_requires_additional_gain_and_actual_draft_labels():
    baseline, structured, reviewed = (
        dataset("baseline", 20),
        dataset("structured", 25),
        dataset("structured_review", 25),
    )
    assert compare_policies(baseline, structured, reviewed)["recommended_mode"] == "structured"
    reviewed = dataset("structured_review", 30)
    reviewed[0].review_status = "completed"
    assert compare_policies(baseline, structured, reviewed)["recommended_mode"] == "structured"
    reviewed[0].draft_causal_correct = True
    assert (
        compare_policies(baseline, structured, reviewed)["recommended_mode"] == "structured_review"
    )


def test_historical_labels_load_without_manufacturing_new_annotations():
    record = dataset("baseline", 20)[0].model_dump()
    for field in (
        "dataset_split",
        "case_origin",
        "independently_adjudicated",
        "evidence_fingerprint",
        "latency_seconds",
        "unsupported_claims",
        "claim_count",
    ):
        record.pop(field)
    loaded = IncidentReview.model_validate(record)
    assert loaded.dataset_split == "unspecified"
    assert loaded.unsupported_claims is None
    assert loaded.independently_adjudicated is False


@pytest.mark.asyncio
async def test_reasoning_benchmark_freezes_inputs_and_separates_inference_modes(
    tmp_path, monkeypatch
):
    from incident_copilot.core.settings import Settings
    from incident_copilot.evaluation import reasoning_benchmark
    from incident_copilot.llm.reasoning import reasoning_summary

    calls = []
    monkeypatch.setattr(reasoning_benchmark, "clean_revision", lambda root: "fixed-revision")

    async def diagnose(payload, config, runtime_context=None):
        calls.append(
            (
                copy.deepcopy(payload),
                config.agentic_enabled,
                config.llm_reasoning_mode,
                runtime_context.execution_id,
            )
        )
        payload["mutated_after_copy"] = True
        return dict(payload, reasoning=reasoning_summary(config.llm_reasoning_mode, 1))

    monkeypatch.setattr(reasoning_benchmark, "run_diagnosis_graph", diagnose)
    config = Settings(_env_file=None, data_dir=tmp_path, index_db_path=tmp_path / "index.db")
    result = await reasoning_benchmark.run_reasoning_comparison(
        {"incident_id": "case"}, tmp_path, config, "pinned-model"
    )
    assert len(calls) == 6
    assert all(payload == calls[0][0] for payload, *_ in calls)
    assert calls[0][0]["source_snapshot"]["revision"] == "fixed-revision"
    assert len({call[3] for call in calls}) == 6
    assert [call[1] for call in calls] == [False] * 3 + [True] * 3
    assert (
        len({arm["evidence_fingerprint"] for modes in result.values() for arm in modes.values()})
        == 1
    )
    assert all(
        arm["model_version"] == "pinned-model"
        for modes in result.values()
        for arm in modes.values()
    )


def test_benchmark_cli_forwards_reasoning_options(tmp_path, monkeypatch):
    from incident_copilot.evaluation import benchmark_runner

    logs = tmp_path / "logs.txt"
    logs.write_text("RuntimeError: failed", encoding="utf-8")
    observed = {}

    async def compare(*args, **kwargs):
        observed.update(kwargs)
        return {"reasoning_comparison": {}}

    monkeypatch.setattr(benchmark_runner, "run_comparison", compare)
    monkeypatch.setattr(
        "sys.argv",
        [
            "benchmark",
            "--repo-root",
            str(tmp_path),
            "--log-file",
            str(logs),
            "--reasoning-modes",
            "--model-version",
            "pinned-model",
            "--output",
            str(tmp_path / "out.json"),
        ],
    )
    benchmark_runner.main()
    assert observed == {"reasoning_modes": True, "model_version": "pinned-model"}


def test_benchmark_templates_preserve_unknown_labels_and_pairing_metadata(tmp_path, monkeypatch):
    import json

    from incident_copilot.evaluation.diagnosis_review import load_reviews, main
    from incident_copilot.llm.reasoning import reasoning_summary
    from incident_copilot.reporting.report_builder import build_report

    artifact = {
        "reasoning_comparison": {
            "single_pass": {
                "structured": {
                    "report": build_report(
                        {"incident_id": "case", "reasoning": reasoning_summary("structured", 1)}
                    ).model_dump(mode="json"),
                    "repository_revision": "abc",
                    "model_version": "pinned-model",
                    "prompt_version": "structured-v1",
                    "evidence_fingerprint": "snapshot",
                    "metrics": {"latency_seconds": 12},
                }
            }
        }
    }
    source, destination = tmp_path / "comparison.json", tmp_path / "review.jsonl"
    source.write_text(json.dumps(artifact), encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "review",
            "template",
            "--benchmark",
            str(source),
            "--inference-mode",
            "single_pass",
            "--reasoning-mode",
            "structured",
            "--output",
            str(destination),
        ],
    )
    main()
    record = load_reviews(destination)[0]
    assert record.model_version == "pinned-model" and record.evidence_fingerprint == "snapshot"
    assert record.latency_seconds == 12
    assert record.evidence_sufficient is None and record.unsupported_claims is None
    assert record.dataset_split == "unspecified" and not record.independently_adjudicated


@pytest.mark.asyncio
async def test_draft_snapshot_preserves_pre_review_outcome(tmp_path, monkeypatch):
    from incident_copilot.core.settings import Settings
    from incident_copilot.evaluation import reasoning_benchmark
    from incident_copilot.llm.reasoning import reasoning_summary
    from tests.unit.test_llm_diagnosis import OUTCOME, hypothesis

    monkeypatch.setattr(reasoning_benchmark, "clean_revision", lambda root: "fixed-revision")

    async def diagnose(payload, config, runtime_context=None):
        draft = {**OUTCOME, "hypotheses": [hypothesis()]}
        return dict(
            payload,
            diagnosis_status="insufficient_evidence",
            diagnosis_reason="The cause remains unverified",
            next_checks=[],
            hypotheses=[],
            reasoning_draft=draft,
            reasoning=reasoning_summary(config.llm_reasoning_mode, 2),
        )

    monkeypatch.setattr(reasoning_benchmark, "run_diagnosis_graph", diagnose)
    config = Settings(_env_file=None, data_dir=tmp_path, index_db_path=tmp_path / "index.db")
    result = await reasoning_benchmark.run_reasoning_comparison(
        {"incident_id": "case"}, tmp_path, config, "pinned-model"
    )
    arm = result["single_pass"]["structured_review"]
    assert arm["report"]["diagnosis_status"] == "insufficient_evidence"
    assert arm["draft_report"]["diagnosis_status"] == "provisional"
    assert "Diagnosis outcome: provisional" in arm["draft_report"]["summary"]
    assert arm["draft_report"]["reasoning"] is None
