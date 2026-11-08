"""Compare prompt policies independently within each inference mode."""

import asyncio
import copy
import hashlib
import json
import random
import time
from uuid import uuid4

from incident_copilot.analysis.runbooks import load_runbooks
from incident_copilot.correlation.log_parser import primary_log
from incident_copilot.graph.diagnosis_graph import run_diagnosis_graph
from incident_copilot.graph.investigation_runtime import InvestigationRuntime
from incident_copilot.graph.nodes.finalize_report import finalize_report_node
from incident_copilot.llm.structured_output import report_hypothesis
from incident_copilot.reporting.report_builder import build_report
from incident_copilot.tools.git_local import clean_revision


async def run_reasoning_comparison(initial_state, root, config, model_version):
    revision = clean_revision(root)
    if not revision or not model_version:
        raise ValueError(
            "Reasoning comparisons require a clean committed snapshot and an explicit model version."
        )
    initial_state = copy.deepcopy(initial_state)
    initial_state["source_snapshot"] = {"repository": str(root.resolve()), "revision": revision}
    if "runbooks" not in initial_state:
        parsed = primary_log(initial_state.get("parsed_log", {}))
        books, limitations = await asyncio.to_thread(
            load_runbooks,
            root,
            revision,
            parsed.get("services", []),
            parsed.get("exception_types", []),
            config,
        )
        initial_state["runbooks"] = books
        initial_state["recommendation_limitations"] = (
            initial_state.get("recommendation_limitations", []) + limitations
        )
    policy = {
        key: value
        for key, value in config.model_dump(mode="json").items()
        if key.startswith(
            ("llm_", "ollama_", "openai_compatible_", "agentic_", "guardrails_", "code_graph_")
        )
        and key not in {"llm_reasoning_mode", "agentic_enabled", "openai_compatible_api_key"}
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            {"initial_state": initial_state, "policy": policy, "model_version": model_version},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    comparisons = {}
    original_ranking = None
    for inference, agentic in (("single_pass", False), ("iterative", True)):
        arms = {}
        modes = ["baseline", "structured", "structured_review"]
        random.Random(fingerprint + inference).shuffle(modes)
        for position, mode in enumerate(modes):
            arm_config = config.model_copy(
                update={"agentic_enabled": agentic, "llm_reasoning_mode": mode}
            )
            started = time.monotonic()
            result = await run_diagnosis_graph(
                copy.deepcopy(initial_state),
                arm_config,
                runtime_context=InvestigationRuntime(root, config.index_db_path, str(uuid4())),
            )
            seconds = time.monotonic() - started
            if clean_revision(root) != revision:
                raise ValueError("Repository snapshot changed during the reasoning comparison.")
            report = build_report(result)
            ranking = [c.model_dump() for c in report.candidates]
            if original_ranking is None:
                original_ranking = ranking
            if ranking != original_ranking:
                raise AssertionError("Reasoning policy changed deterministic candidate ranking.")
            draft_report = None
            if result.get("reasoning_draft"):
                draft = result["reasoning_draft"]
                draft_report = build_report(
                    finalize_report_node(
                        dict(
                            result,
                            reasoning=None,
                            investigation=None,
                            guardrails=None,
                            validation_errors=[],
                            **{
                                k: draft[k]
                                for k in ("diagnosis_status", "diagnosis_reason", "next_checks")
                            },
                            hypotheses=[report_hypothesis(h) for h in draft["hypotheses"]],
                        )
                    )
                ).model_dump(mode="json")
            arms[mode] = {
                "execution_order": position,
                "report": report.model_dump(mode="json"),
                "draft_report": draft_report,
                "repository_revision": revision,
                "model_version": model_version,
                "evidence_fingerprint": fingerprint,
                "inference_mode": inference,
                "prompt_version": report.reasoning.prompt_version if report.reasoning else None,
                "metrics": {
                    "latency_seconds": seconds,
                    "model_calls": report.reasoning.model_calls if report.reasoning else None,
                    "guardrail_model_calls": report.guardrails.model_calls
                    if report.guardrails
                    else 0,
                    "validation_failures": len(report.validation_errors),
                    "fallback_reason": report.fallback_reason,
                    "engine": report.engine,
                },
                "review_labels": {
                    "causal_correct": None,
                    "unsupported_claims": None,
                    "draft_causal_correct": None,
                    "evidence_sufficient": None,
                },
            }
        comparisons[inference] = arms
    return comparisons
