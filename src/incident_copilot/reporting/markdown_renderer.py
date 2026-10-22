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
