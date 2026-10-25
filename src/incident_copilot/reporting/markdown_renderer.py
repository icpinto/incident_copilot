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
    if report.fallback_reason:
        parts.append(f"- Fallback reason: `{report.fallback_reason}`")
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
    parts.extend(["", "## Summary", report.summary, "", "## Top Candidates"])
    for c in report.candidates:
        parts.extend(
            [
                f"### `{c.file_path}`",
                f"- score: {c.score}",
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
                h.explanation,
                "",
                f"- confidence: {h.confidence}",
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
    if report.retrieved_evidence:
        parts.extend(["## Retrieved Evidence", ""])
        for item in report.retrieved_evidence:
            location = item.file_path or item.repository
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
            if item.limitations:
                parts.append(f"- Limitations: {'; '.join(item.limitations)}")
            if item.truncated:
                parts.append("- Content clipped; further bounded reads may be required.")
            # Pick a delimiter longer than any code fence in retrieved source.
            content = item.content or (json.dumps(item.data, ensure_ascii=False, indent=2) if item.data else "")
            fence = "`" * max(3, max((len(p) for p in re.findall(r"`+", content)), default=0) + 1)
            parts.extend(["", fence, content, fence, ""])
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
