import json
from pathlib import Path

from incident_copilot.schemas.diagnosis import DiagnosisReport


def render_json(report: DiagnosisReport, output_path: Path | None = None) -> str:
    text = json.dumps(report.model_dump(), indent=2)
    if output_path:
        output_path.write_text(text, encoding="utf-8")
    return text
