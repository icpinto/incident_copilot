"""Incident association and source ownership must not imply runtime causation."""
import json
from pathlib import Path

import pytest

from incident_copilot.core.settings import Settings
from incident_copilot.correlation.candidate_ranker import rank_candidates
from incident_copilot.correlation.log_parser import group_events, parse_logs, primary_log
from incident_copilot.graph import investigation
from incident_copilot.graph.diagnosis_graph import run_diagnosis_graph
from incident_copilot.graph.investigation_runtime import InvestigationRuntime
from incident_copilot.graph.nodes import build_hypotheses
from incident_copilot.graph.nodes.build_hypotheses import build_diagnosis_prompt
from incident_copilot.graph.nodes.normalize_logs import normalize_logs_node
from incident_copilot.indexing.storage import IndexStorage
from incident_copilot.reporting.markdown_renderer import render_markdown
from incident_copilot.reporting.report_builder import build_report
from incident_copilot.schemas.index import FileRecord, SymbolRecord
from incident_copilot.workflows.log_context import hydrate_parsed_log, offload_parsed_log

FIXTURE = Path(__file__).parents[1] / "fixtures" / "incident_timeline" / "correlated.jsonl"
OUTCOME = {
    "diagnosis_status": "provisional",
    "diagnosis_reason": "The incident evidence supports this provisional causal mechanism.",
    "next_checks": ["Verify against the failing runtime input"],
}


REVISION = "a" * 40


@pytest.fixture
def indexed(tmp_path):
    root = tmp_path / "source"
    records = []
    for service, file, symbol in (("database", "query", "query"), ("api", "handler", "handle"), ("worker", "job", "job")):
        relative = f"services/{service}/{file}.py"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"def {symbol}():\n    raise RuntimeError('failed')\n")
        records.append(FileRecord(file_path=relative, language="python", service_hints=[service],
                                  symbols=[SymbolRecord(name=symbol, kind="function", file_path=relative, line_start=1, line_end=2)]))
    index = tmp_path / "index.sqlite"
    IndexStorage(index).replace_snapshot(root, [], records, REVISION)
    return root, index


def test_structured_aliases_precedence_and_timestamp():
    parsed = parse_logs(json.dumps({
        "msg": "wrong | 2026-10-26T09:00:00Z traceId=embedded TypeError: bad",
        "@timestamp": "2026-10-26T11:00:00+01:00", "service": {"name": "right"}, "trace_id": "outer",
        "requestId": "request",
    }))
    event = parsed.incident_context.events[0]
    assert (event.service, event.trace_id, event.request_id) == ("right", "outer", "request")
    assert event.timestamp_utc == "2026-10-26T10:00:00+00:00"
    assert len(event.limitations) == 3
    assert event.exceptions[0].type == "TypeError"
    assert parsed.services == ["right"]


def test_structured_aliases_skip_mistyped_values():
    event = parse_logs(json.dumps({"message": 5, "msg": "ValueError: failed", "service": False,
                                  "service_name": "api", "trace_id": None, "traceId": "shared"})).incident_context.events[0]
    assert event.message == "ValueError: failed" and event.service == "api" and event.trace_id == "shared"


@pytest.mark.parametrize("stamp", ["2026-10-26T10:00:00", "10:00:00", "2026-99-99T10:00:00Z"])
def test_timestamps_never_assume_host_timezone(stamp):
    event = parse_logs(f"api | {stamp} ValueError: failed").incident_context.events[0]
    assert event.timestamp == stamp and event.timestamp_utc is None


def test_malformed_json_and_deterministic_ids():
    text = 'api | {"message":"ValueError: broken"\nValueError: independent'
    first = parse_logs(text)
    assert first.model_dump() == parse_logs(text).model_dump()
    assert len(first.incident_context.groups) == 2
    assert "Malformed JSON" in first.incident_context.events[0].limitations[0]


def test_interleaved_service_traces_and_ambiguous_unprefixed_frame():
    text = ('api | Traceback (most recent call last):\nworker | Traceback (most recent call last):\n'
            '  File "/app/unknown.py", line 3, in run\n'
            'api |   File "/app/api.py", line 2, in run\nworker |   File "/app/worker.py", line 2, in run\n'
            'api | ValueError: api\nworker | RuntimeError: worker')
    context = parse_logs(text).incident_context
    assert len(context.events) == 3
    events = {e.service: e for e in context.events}
    assert events["api"].stack_frames[0].file_path == "/app/api.py"
    assert events["worker"].stack_frames[0].file_path == "/app/worker.py"
    assert events[None].stack_frames[0].file_path == "/app/unknown.py"
    assert events[None].limitations


@pytest.mark.parametrize("marker,relation", [
    ("The above exception was the direct cause of the following exception:", "caused_by"),
    ("During handling of the above exception, another exception occurred:", "during_handling"),
])
def test_python_chain_keeps_exception_owned_frames(marker, relation):
    text = ('api | Traceback (most recent call last):\n  File "/app/first.py", line 2, in first\nValueError: first\n'
            f'\n{marker}\n\nTraceback (most recent call last):\n  File "/app/second.py", line 3, in second\nRuntimeError: second')
    context = parse_logs(text).incident_context
    assert len(context.events) == 1
    first, second = context.events[0].exceptions
    assert second.relation == relation and second.previous_id == first.id
    assert first.stack_frames[0].file_path == "/app/first.py"
    assert second.stack_frames[0].file_path == "/app/second.py"


@pytest.mark.parametrize("marker", ["Caused by:", "[cause]:"])
def test_node_and_java_causes_do_not_swallow_next_independent_error(marker):
    text = (f'Error: outer\n    at handle (/app/a.ts:2:1)\n{marker} TypeError: inner\n'
            '    at charge (/app/b.ts:3:1)\nValueError: separate')
    context = parse_logs(text).incident_context
    assert len(context.events) == 2
    first, second = context.events[0].exceptions
    assert second.previous_id == first.id and second.relation == "cause"
    assert second.stack_frames[0].file_path == "/app/b.ts"


def test_timestamped_trace_lines_form_one_event_but_new_trace_is_separate():
    text = ('api | 2026-10-26T10:00:00Z Traceback (most recent call last):\n'
            'api | 2026-10-26T10:00:01Z   File "/app/main.py", line 2, in run\n'
            'api | 2026-10-26T10:00:02Z ValueError: first\n'
            'api | 2026-10-26T10:00:03Z Traceback (most recent call last):\n'
            'api | 2026-10-26T10:00:04Z RuntimeError: independent')
    events = parse_logs(text).incident_context.events
    assert len(events) == 2
    assert events[0].exceptions[0].stack_frames[0].file_path == "/app/main.py"


def test_conservative_id_grouping_rejects_request_bridges():
    lines = [
        {"service": "api", "trace_id": "one", "request_id": "r", "message": "ValueError: one"},
        {"service": "db", "trace_id": "two", "request_id": "r", "message": "ValueError: two"},
        {"service": "worker", "request_id": "r", "message": "ValueError: ambiguous"},
        {"service": "cache", "request_id": "r", "message": "ValueError: also ambiguous"},
    ]
    context = parse_logs("\n".join(json.dumps(v) for v in lines)).incident_context
    assert len(context.groups) == 4 and context.limitations
    lines[1]["trace_id"] = "one"
    context = parse_logs("\n".join(json.dumps(v) for v in lines)).incident_context
    assert len(context.groups) == 1 and context.groups[0].grouping_basis == "trace_id"


def test_requests_can_associate_cross_service_but_time_and_service_cannot():
    text = ('api | 2026-10-26T10:00:00Z request_id=shared ValueError: first\n'
            'db | 2026-10-26T10:00:00Z request_id=shared ValueError: second\n'
            'api | 2026-10-26T10:00:00Z ValueError: independent\n'
            'api | 2026-10-26T10:00:00Z ValueError: independent')
    context = parse_logs(text).incident_context
    assert [len(g.event_ids) for g in context.groups] == [2, 1, 1]


def test_primary_group_excludes_unrelated_ranking_and_prompt_evidence(indexed):
    root, index = indexed
    logs = FIXTURE.read_text()
    candidates, parsed = rank_candidates(root, index, logs)
    clean = "\n".join(line for line in logs.splitlines() if "unrelated" not in line)
    isolated, _ = rank_candidates(root, index, clean)
    assert [(c.file_path, c.score) for c in candidates.items] == [(c.file_path, c.score) for c in isolated.items]
    assert all(c.group_id == parsed.incident_context.primary_group_id for c in candidates.items)
    assert candidates.items[0].event_ids
    state = normalize_logs_node({"parsed_log": parsed.model_dump(), "candidates": [c.model_dump() for c in candidates.items]})
    assert state["likely_service"] == candidates.items[0].service_name
    assert state["likely_error_class"] == {"database": "ConnectionError", "api": "TimeoutError"}[state["likely_service"]]
    prompt = build_diagnosis_prompt(state)
    assert "upstream unavailable" in prompt and "unrelated-sentinel" not in prompt
    assert "1 other group(s) excluded" in prompt


def test_service_hint_selects_observed_group_without_manufacturing_ownership(indexed):
    root, index = indexed
    _, parsed = rank_candidates(root, index, FIXTURE.read_text(), user_hint_service="worker")
    assert [e.service for e in group_events(parsed.incident_context, parsed.incident_context.primary_group_id)] == ["worker"]
    _, unmatched = rank_candidates(root, index, "unknown | ValueError: mystery", user_hint_service="api")
    assert unmatched.services == ["unknown"]
    assert unmatched.incident_context.limitations


def test_error_groups_precede_info_and_zero_score_ties_use_input_order(tmp_path):
    index = tmp_path / "empty.sqlite"
    IndexStorage(index).replace_snapshot(tmp_path, [], [], REVISION)
    _, parsed = rank_candidates(tmp_path, index, "api | informational\nworker | ValueError: first\napi | ValueError: second")
    assert primary_log(parsed.model_dump())["services"] == ["worker"]
    assert "no positive" in parsed.incident_context.selection_reason


def test_exports_and_legacy_reports(indexed):
    root, index = indexed
    candidates, parsed = rank_candidates(root, index, FIXTURE.read_text())
    state = normalize_logs_node({"parsed_log": parsed.model_dump(), "candidates": [c.model_dump() for c in candidates.items]})
    report = build_report(state)
    text = render_markdown(report)
    assert "Incident Timeline" in text and "Original lines: 1-1" in text
    assert "upstream unavailable" in text and "unrelated-sentinel" not in text
    assert "unrelated-sentinel" in report.model_dump_json()
    assert build_report({}).incident_context is None
    legacy = {"parsed_log": {"services": ["z", "a"], "exception_types": ["ValueError"]}}
    assert normalize_logs_node(legacy)["likely_service"] == "z"


def test_offloaded_context_is_stable_hydrated_and_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("IC_CONTROL_DB_PATH", str(tmp_path / "control.sqlite"))
    parsed = parse_logs(FIXTURE.read_text()).model_dump()
    first = offload_parsed_log(parsed)
    assert first == offload_parsed_log(parsed)
    assert "unrelated-sentinel" not in json.dumps(first)
    assert hydrate_parsed_log(first) == parsed
    from incident_copilot.dashboard.store import ControlStore
    assert ControlStore(tmp_path / "control.sqlite").list("revisions", exclude_id_prefix="incident-context-") == []
    with pytest.raises(ValueError, match="unavailable"):
        hydrate_parsed_log({"incident_context_ref": "incident-context-" + "0" * 64})
    with pytest.raises(ValueError, match="Invalid"):
        hydrate_parsed_log({"incident_context_ref": "../bad"})


def test_exception_with_source_path_and_simplified_python_frames_keep_ownership():
    event = parse_logs('File "/app/main.py", line 3, in run\nValueError: could not read /app/config.py').incident_context.events[0]
    assert event.exceptions[0].type == "ValueError"
    assert event.exceptions[0].stack_frames[0].file_path == "/app/main.py"


def test_python_source_lines_are_not_exception_headers_and_errors_stay_separate():
    text = ('api | Traceback (most recent call last):\n'
            '  File "/app/main.py", line 3, in run\n'
            '    raise ValueError("bad")\nValueError: bad\n'
            '    ERROR another independent failure')
    events = parse_logs(text).incident_context.events
    assert len(events) == 2
    assert [e.type for e in events[0].exceptions] == ["ValueError"]
    assert events[0].line_end == 4


def test_initial_timeline_evidence_is_bounded_and_omissions_are_explicit(indexed):
    root, index = indexed
    logs = "\n".join(json.dumps({"service": "api", "trace_id": "large", "message": f"ValueError: {number} " + "x" * 3000}) for number in range(100))
    candidates, parsed = rank_candidates(root, index, logs)
    state = investigation._bootstrap({"parsed_log": parsed.model_dump(), "candidates": [c.model_dump() for c in candidates.items]},
                                     {"repository": str(root), "revision": REVISION}, Settings(_env_file=None))
    assert state["omitted_initial_context_count"] > 0
    assert sum(len(investigation.encoded(v)) for v in state["evidence_records"].values()) <= investigation.MAX_EVIDENCE_BYTES
    assert any(e["truncated"] for e in state["evidence_records"].values())
    budget = {"started_at": 0, "rounds": 0, "model_calls": 0, "tool_calls": 0}
    shown = investigation.build_investigation_prompt(state, budget)
    assert len(shown.encode()) < investigation.MAX_PROMPT_BYTES
    assert json.loads(shown)["omitted_initial_context_count"] > 0
    summary = investigation._public(state, budget, "model_finished")["investigation"]
    assert summary["incomplete"] and summary["unresolved_gaps"]


@pytest.mark.asyncio
async def test_both_reasoning_modes_and_checkpoint_resume_keep_scope(indexed, tmp_path, monkeypatch):
    root, index = indexed
    candidates, parsed = rank_candidates(root, index, FIXTURE.read_text())
    payload = {"incident_id": "timeline", "parsed_log": parsed.model_dump(), "candidates": [c.model_dump() for c in candidates.items]}
    from incident_copilot.analysis import evidence_tools
    monkeypatch.setattr(investigation, "clean_revision", lambda _: REVISION)
    monkeypatch.setattr(evidence_tools, "clean_revision", lambda _: REVISION)
    calls = []

    class Model:
        async def complete(self, prompt, **kwargs):
            calls.append(prompt)
            assert "unrelated-sentinel" not in prompt
            h = {"rank": 1, "title": "Upstream failure", "explanation": "Inspect database availability", "evidence": ["ConnectionError"], "confidence": .5, "next_checks": ["Check database"]}
            if prompt.startswith("{"):
                evidence = json.loads(prompt)["evidence"]
                assert any(e["kind"] == "exception_chain" for e in evidence)
                h.update(id="h1", evidence_refs=[next(e["id"] for e in evidence if e["kind"] == "log_event")], contradicting_evidence_refs=[])
                return json.dumps({**OUTCOME, "action": "finish", "hypotheses": [h], "requests": [], "unresolved_gaps": []})
            return json.dumps({**OUTCOME, "hypotheses": [h]})

    monkeypatch.setattr(build_hypotheses, "get_client", lambda *_, **_kwargs: Model())
    monkeypatch.setattr(investigation, "get_client", lambda *_, **_kwargs: Model())
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", index_db_path=index)
    single = await run_diagnosis_graph(payload, settings)
    runtime = InvestigationRuntime(root, index, "timeline-execution")
    iterative = await run_diagnosis_graph(payload, settings.model_copy(update={"agentic_enabled": True}), runtime)
    resumed = await run_diagnosis_graph({**payload, "parsed_log": {}}, settings.model_copy(update={"agentic_enabled": True}), runtime)
    assert len(calls) == 2
    assert single["incident_context"] == iterative["incident_context"] == resumed["incident_context"]
    assert iterative["hypotheses"][0]["evidence_refs"]
    assert "unrelated-sentinel" not in json.dumps(iterative["retrieved_evidence"])
