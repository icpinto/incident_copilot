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
    text = "\n".join(parts)
    if output_path:
        output_path.write_text(text, encoding="utf-8")
    return text
