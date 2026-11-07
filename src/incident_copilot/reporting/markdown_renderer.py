import json
import re
from pathlib import Path

from incident_copilot.schemas.diagnosis import DiagnosisReport


def render_markdown(report: DiagnosisReport, output_path: Path | None = None) -> str:
    parts = [
        "# Incident Diagnosis Report",
        "",
        f"- Incident ID: `{report.incident_id}`",
        f"- Likely service: `{report.likely_service or 'unknown'}`",
        f"- Likely error class: `{report.likely_error_class or 'unknown'}`",
    ]
    if report.repository_source:
        source = report.repository_source
        parts.extend([f"- Repository: `{source.get('remote_url', '')}`",
                      f"- Branch: `{source.get('branch', '')}`",
                      f"- Source commit: `{source.get('commit', '')}`"])
    if report.deployment_context:
        deployed = report.deployment_context
        resolution = report.source_resolution
        parts.extend(["", "## Deployed Version", "",
                      f"- Environment: `{deployed.environment}`; service: `{deployed.service}`",
                      f"- Collector: `{deployed.collector_id}`; container: `{deployed.container_id}`",
                      f"- Release version: `{deployed.version or 'unknown'}`",
                      f"- Declared commit: `{deployed.revision or 'unknown'}`",
                      f"- Analyzed commit: `{resolution.analyzed_commit or 'unknown'}`",
                      f"- Source resolution: {resolution.status}",
                      f"- Image reference: `{deployed.image_ref or 'unknown'}`",
                      f"- Image ID: `{deployed.image_id or 'unknown'}`",
                      f"- Registry digests: `{', '.join(deployed.repo_digests) or 'unknown'}`",
                      "- Labels declare build provenance; runtime contents and causation are not verified.", ""])
        if resolution.reason:
            parts.extend([resolution.reason, ""])
        if deployed.runtime_config:
            config = deployed.runtime_config
            parts.extend(["## Observed Runtime Configuration", "",
                          "Collector observation; sensitive values are redacted and mount contents are unavailable.", ""])
            if config.status == "available":
                rendered = json.dumps(config.model_dump(mode="json"), indent=2)
                fence = "`" * max(3, max((len(p) for p in re.findall(r"`+", rendered)), default=0) + 1)
                parts.extend([fence + "json", rendered, fence, ""])
            else:
                parts.extend(["Container configuration could not be inspected.", ""])
    parts.extend([f"- Diagnosis outcome: {report.diagnosis_status.replace('_', ' ').capitalize()}"])
    if report.diagnosis_reason:
        parts.append(f"- Reason: {report.diagnosis_reason}")
    if report.next_checks:
        parts.extend(["", "## Next Checks", "", *[f"- {check}" for check in report.next_checks]])
    if report.fallback_reason:
        parts.append(f"- Fallback reason: `{report.fallback_reason}`")
    if report.incident_context is not None:
        context = report.incident_context
        parts.extend(["", "## Incident Timeline", "",
                      f"- Primary group: `{context.primary_group_id or 'none'}`",
                      f"- Selection: {context.selection_reason or 'not selected'}",
                      "- Diagnosis covers the primary group; other groups are context only.",
                      "- Shared IDs and chronology establish association, not causation.", ""])
        for group in context.groups:
            label = "primary" if group.id == context.primary_group_id else "excluded context"
            parts.append(f"- `{group.id}` ({label}): {len(group.event_ids)} events; "
                         f"services: {', '.join(group.services) or 'unknown'}; grouped by {group.grouping_basis}")
        for limitation in context.limitations:
            parts.append(f"- Limitation: {limitation}")
        from incident_copilot.correlation.log_parser import group_events
        for event in group_events(context, context.primary_group_id):
            parts.extend(["", f"### Event {event.id}",
                          f"- Original lines: {event.line_start}-{event.line_end}",
                          f"- Service: `{event.service or 'unknown'}`; timestamp: `{event.timestamp or 'unknown'}`"])
            for exception in event.exceptions:
                parts.append(f"- Exception: `{exception.type}`; "
                             f"relation: {exception.relation or 'none'}; previous: {exception.previous_id or 'none'}")
            for limitation in event.limitations:
                parts.append(f"- Limitation: {limitation}")
            fence = "`" * max(3, max((len(p) for p in re.findall(r"`+", event.message)), default=0) + 1)
            parts.extend(["", fence, event.message, fence, ""])
    if report.guardrails is not None:
        checks = report.guardrails
        parts.extend(["", "## Guardrails", "",
                      f"- Mode: {checks.mode}; policy: `{checks.policy_version}`",
                      f"- Checks: {checks.checks}; additional model calls: {checks.model_calls}",
                      f"- Check duration: {checks.duration_ms / 1000:.2f} seconds",
                      "- Observation mode records findings without blocking." if checks.mode == "observe"
                      else "- Enforcement stops processing when a check rejects content or is unavailable."])
        for event in checks.events:
            parts.append(f"- {event.stage}, assessment {event.model_call}: {event.status} "
                         f"(`{event.code}`; {event.duration_ms:.0f} ms)")
    if report.validation_errors:
        parts.extend(["", "## Analysis Validation Errors", ""])
        for failure in report.validation_errors:
            context = failure.stage.replace("_", " ")
            if failure.model_call is not None:
                context += f"; model call {failure.model_call}"
            if failure.request_id:
                context += f"; request {failure.request_id}"
            parts.append(f"- {context} ({failure.kind})")
            for issue in failure.issues:
                path = ".".join(str(part) for part in issue.path) or "$"
                parts.append(f"  - `{path}`: `{issue.code}` — {issue.message}")
            if failure.total_issues > len(failure.issues):
                parts.append(f"  - {failure.total_issues - len(failure.issues)} additional errors omitted.")
    if report.investigation is not None:
        investigation = report.investigation
        parts.extend([
            "", "## Investigation", "",
            f"- Status: {'incomplete' if investigation.incomplete else 'complete'}",
            f"- Stop reason: {investigation.stop_reason}",
            f"- Retrieval rounds: {investigation.rounds}",
            f"- Model calls: {investigation.model_calls}; tool requests: {investigation.tool_calls}",
            f"- Duration: {investigation.elapsed_ms / 1000:.2f} seconds",
        ])
        if investigation.unresolved_gaps:
            parts.extend(["", "Unresolved evidence gaps:"])
            parts.extend(f"- {gap}" for gap in investigation.unresolved_gaps)
    parts.extend(["", "## Summary", report.summary, "", "## Localization", "", "Localization strength is a raw correlation score, not a probability."])
    for c in report.candidates:
        parts.extend(
            [
                f"### `{c.file_path}`",
                f"- Localization strength: {c.score}",
                f"- symbol: `{c.symbol_name or 'n/a'}`",
                f"- service: `{c.service_name or 'n/a'}`",
                f"- reasons: {'; '.join(c.reasons[:6])}",
                "",
                "```",
                (c.snippet or "").strip(),
                "```",
                "",
            ]
        )
    if report.graph_context is not None:
        graph = report.graph_context
        parts.extend([
            "## Static Code Graph", "",
            f"- Provider: {graph.provider} {graph.provider_version or 'unknown'}",
            f"- Status: {graph.status}; completeness: {graph.completeness}",
            f"- Repository revision: `{graph.repo_revision or 'unknown'}`",
            f"- Indexed revision: `{graph.indexed_revision or 'unknown'}`",
        ])
        if graph.reason:
            parts.append(f"- Reason: {graph.reason}")
        symbols = {s.uid: s for s in graph.symbols}
        for edge in graph.relationships:
            source, target = symbols.get(edge.source_uid), symbols.get(edge.target_uid)
            if source and target:
                parts.append(f"- `{source.name}` ({source.file_path}:{source.line_start or '?'}) calls "
                             f"`{target.name}` ({target.file_path}:{target.line_start or '?'})")
        for seed in graph.seeds:
            if seed.reason:
                parts.append(f"- {seed.file_path} / {seed.symbol_name}: {seed.reason}")
        parts.extend(["", "Static paths describe possible execution, not observed causation.", ""])
    parts.append("## Hypotheses")
    for h in report.hypotheses:
        parts.extend(
            [
                f"### {h.rank}. {h.title}",
                f"- Suspected failure layer: {h.failure_layer}",
                h.explanation,
                "",
                (f"- Diagnosis confidence: {h.confidence:.0%} (calibrated)"
                 if h.confidence_status == "calibrated" and h.confidence is not None
                 else f"- Diagnosis confidence: {h.confidence_status}; no calibrated probability available"),
                f"- evidence: {'; '.join(h.evidence)}",
                f"- next checks: {'; '.join(h.next_checks)}",
                "",
            ]
        )
        if h.evidence_refs:
            parts.extend([
                "- citations: " + "; ".join(
                    f"[{ref}](#evidence-{ref})" for ref in h.evidence_refs
                ), "",
            ])
        if h.action_plan:
            plan = h.action_plan
            parts.extend(["#### Supporting evidence", ""])
            for claim in plan.supporting_evidence:
                parts.append(f"- {claim.claim}: " + ", ".join(f"[{ref}](#evidence-{ref})" for ref in claim.evidence_refs))
            parts.extend(["", "#### Missing evidence", "", *[f"- {gap}" for gap in plan.missing_evidence]])
            if not plan.missing_evidence:
                parts.append("No additional evidence gaps identified by this assessment; uncertainty remains.")
            for check in plan.checks:
                parts.extend(["", f"#### Next check: {check.instruction}", f"- Action ID: `{check.id}`",
                              f"- Where: {check.context}", f"- Collect: {check.evidence_to_collect}",
                              f"- Predicted supporting result: {check.supports_hypothesis}",
                              f"- Predicted weakening result: {check.weakens_hypothesis}"])
                if check.command:
                    fence = "`" * max(3, max((len(p) for p in re.findall(r"`+", check.command)), default=0) + 1)
                    parts.extend(["", fence, check.command, fence])
                if check.placeholders:
                    parts.append("- Resolve placeholders: " + "; ".join(check.placeholders))
            parts.extend(["", "#### Relevant runbooks", ""])
            for ref in plan.runbooks:
                parts.append(f"- [{ref.runbook_id}](#runbook-{ref.runbook_id}), {ref.section}: {ref.reason}")
            if not plan.runbooks:
                parts.append("No matching runbook.")
            for mitigation in plan.mitigations:
                origin = "Repository runbook" if mitigation.runbook_ids else "Unreviewed model proposal"
                parts.extend(["", f"#### Mitigation: {mitigation.title}", f"- Action ID: `{mitigation.id}`",
                              f"- Provenance: {origin}", f"- Intended effect: {mitigation.intended_effect}",
                              f"- Prerequisites: {'; '.join(mitigation.prerequisites)}",
                              f"- Risks: {'; '.join(mitigation.risks)}",
                              f"- Affected services: {'; '.join(mitigation.affected_services)}"])
                if mitigation.missing_prerequisites:
                    parts.append("- Blocked: " + "; ".join(mitigation.missing_prerequisites))
                parts.extend(["", "Steps:", *[f"{i}. {step}" for i, step in enumerate(mitigation.steps, 1)],
                              "", "Rollback:", *[f"- {step}" for step in mitigation.rollback],
                              "", "Recovery verification (predicted):",
                              f"- Measure: {mitigation.verification.measure}",
                              f"- Success condition: {mitigation.verification.success_condition}",
                              f"- Observation period: {mitigation.verification.observation_period}"])
    if report.recommendation_limitations:
        parts.extend(["", "## Recommendation limitations", "", *[f"- {item}" for item in report.recommendation_limitations]])
    if report.runbooks:
        parts.extend(["", "## Repository runbook snapshots", ""])
        for book in report.runbooks:
            parts.extend([f'<a id="runbook-{book.id}"></a>', f"### {book.title}",
                          f"- Source: `{book.file_path}`; revision: `{book.revision or 'unknown'}`",
                          f"- Content SHA-256: `{book.content_hash}`"])
            for section in book.sections:
                fence = "`" * max(3, max((len(p) for p in re.findall(r"`+", section.content)), default=0) + 1)
                parts.extend(["", f"#### {section.heading} (lines {section.line_start}-{section.line_end})",
                              fence, section.content, fence])
    if report.follow_up:
        parts.extend(["", "## Responder follow-through", "", "Responder-reported observations; the saved diagnosis is unchanged.", ""])
        for event in report.follow_up:
            parts.extend([f"- {event.recorded_at}: `{event.action_id}` - {event.status} ({event.responder or 'Responder not specified'})",
                          f"  - Observations: {event.observations or 'None recorded'}"])
    if report.retrieved_evidence:
        parts.extend(["## Retrieved Evidence", ""])
        for item in report.retrieved_evidence:
            location = item.file_path or item.repository or (f"{item.runtime.collector_id}/{item.runtime.project}/{item.runtime.service}" if item.runtime else "unknown")
            if item.line_start is not None:
                location += f":{item.line_start}"
                if item.line_end is not None:
                    location += f"-{item.line_end}"
            parts.extend([
                f'<a id="evidence-{item.id}"></a>',
                f"### {item.id}",
                f"- Kind: {item.kind}",
                f"- Source: `{location}`; revision: `{item.revision or 'unknown'}`",
            ])
            if item.request_id:
                parts.append(f"- Request: `{item.request_id}`")
            if item.runtime:
                parts.extend([f"- Environment: {item.runtime.environment}",
                    f"- Requested window: {item.runtime.requested_start.isoformat()} to {item.runtime.requested_end.isoformat()}",
                    f"- Observed at: {item.runtime.observed_at.isoformat()}; coverage: {item.runtime.coverage}"])
            if item.limitations:
                parts.append(f"- Limitations: {'; '.join(item.limitations)}")
            if item.truncated:
                parts.append("- Content clipped; further bounded reads may be required.")
            # Pick a delimiter longer than any code fence in retrieved source.
            content = item.content or (json.dumps(item.data, ensure_ascii=False, indent=2) if item.data else "")
            fence = "`" * max(3, max((len(p) for p in re.findall(r"`+", content)), default=0) + 1)
            parts.extend(["", fence, content, fence, ""])
    if report.runtime_collection:
        parts.extend(["## Runtime Collection", ""])
        for result in report.runtime_collection:
            parts.append(f"- {result['capability']}: {result['status']}; {result.get('reason') or ''} ({result.get('duration_ms', 0)} ms)")
    if report.investigation_events:
        parts.extend(["## Investigation Actions", ""])
        for event in report.investigation_events:
            parts.append(f"- {event.tool or event.kind}: {event.status} ({event.duration_ms} ms)")
            if event.question:
                parts.append(f"  - Question: {event.question}")
            if event.reason:
                parts.append(f"  - Result: {event.reason}")
            if event.evidence_ids:
                parts.append("  - Evidence: " + ", ".join(
                    f"[{ref}](#evidence-{ref})" for ref in event.evidence_ids
                ))
    text = "\n".join(parts)
    if output_path:
        output_path.write_text(text, encoding="utf-8")
    return text
