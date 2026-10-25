from incident_copilot.dashboard.models import IncidentEvidence
from incident_copilot.reporting.markdown_renderer import render_markdown
from incident_copilot.reporting.report_builder import build_report
from incident_copilot.schemas.diagnosis import DiagnosisReport


def historical_state():
    return {
        "incident_id": "cache-incident",
        "summary": "Cache path failed.",
        "hypotheses": [{
            "rank": 1, "title": "Cache path", "explanation": "Inspect path construction.",
            "evidence": ["cache.py:2"], "confidence": .6, "next_checks": ["Reproduce input"],
        }],
    }


def test_historical_report_and_evidence_default_to_empty_investigation():
    report = build_report(historical_state())
    historical_json = report.model_dump(exclude={"retrieved_evidence", "investigation", "investigation_events"})
    historical_json["hypotheses"][0].pop("evidence_refs")
    restored = DiagnosisReport.model_validate(historical_json)
    assert restored.investigation is None and restored.retrieved_evidence == []
    assert restored.hypotheses[0].evidence_refs == []
    assert "Retrieved Evidence" not in render_markdown(restored)
    assert IncidentEvidence.model_validate({"raw_log": "old incident"}).investigation is None


def test_reports_preserve_investigation_citations_limits_and_actions():
    state = historical_state()
    state["hypotheses"][0]["evidence_refs"] = ["ev-cache"]
    state.update({
        "retrieved_evidence": [{
            "id": "ev-cache", "kind": "source", "repository": "/repo", "revision": "abc123",
            "file_path": "cache.py", "line_start": 2, "line_end": 3,
            "content": 'def _path():\n    return "```"', "limitations": ["parser_fallback"],
            "request_id": "req-cache", "truncated": True,
        }],
        "investigation": {
            "rounds": 2, "model_calls": 3, "tool_calls": 4, "elapsed_ms": 1200,
            "stop_reason": "model_finished", "incomplete": True,
            "unresolved_gaps": ["Need runtime input"],
        },
        "investigation_events": [{
            "id": "action-cache", "kind": "retrieval", "tool": "read_source",
            "question": "How is the path built?", "status": "ok",
            "evidence_ids": ["ev-cache"], "duration_ms": 12,
        }],
    })
    report = build_report(state)
    restored = DiagnosisReport.model_validate_json(report.model_dump_json())
    assert restored == report
    markdown = render_markdown(report)
    assert "[ev-cache](#evidence-ev-cache)" in markdown
    assert '<a id="evidence-ev-cache"></a>' in markdown
    assert "abc123" in markdown
    assert "Need runtime input" in markdown and "parser_fallback" in markdown
    assert "Content clipped" in markdown and "How is the path built?" in markdown
    assert '\n````\ndef _path():' in markdown
    evidence = IncidentEvidence.model_validate({key: state[key] for key in (
        "retrieved_evidence", "investigation", "investigation_events",
    )})
    assert evidence.retrieved_evidence == report.retrieved_evidence


def test_markdown_preserves_structured_evidence_with_empty_source_content():
    state = historical_state()
    state["retrieved_evidence"] = [{"id": "logs", "kind": "log", "repository": "/repo",
        "revision": "abc", "data": {"error_lines": ["Cache directory could not be opened"]}}]
    markdown = render_markdown(build_report(state))
    assert '"error_lines"' in markdown
    assert "Cache directory could not be opened" in markdown
