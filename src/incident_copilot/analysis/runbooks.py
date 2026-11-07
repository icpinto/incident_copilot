"""Bounded runbook discovery from the analysis repository, never from model paths."""
import hashlib
import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from incident_copilot.analysis.evidence_tools import redact_evidence
from incident_copilot.schemas.recommendations import RunbookSection, RunbookSnapshot
from incident_copilot.schemas.runtime import FailureLayer

MAX_RUNBOOK_BYTES = 8192
REQUIRED_SECTIONS = {"diagnosis", "prerequisites", "mitigation", "risks", "rollback", "verification"}


class Metadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=300)
    services: list[str] = Field(min_length=1, max_length=20)
    error_classes: list[str] = Field(max_length=20)
    failure_layers: list[FailureLayer] = Field(max_length=5)


def load_runbooks(root: Path | None, revision: str | None, services: list[str],
                  error_classes: list[str], settings) -> tuple[list[dict], list[str]]:
    if root is None:
        return [], ["Runbooks unavailable: source snapshot is unavailable."]
    try:
        root = root.resolve()
        directory = root / "docs" / "runbooks"
        if not directory.exists():
            return [], []
    except (OSError, RuntimeError):
        return [], ["Runbook directory could not be read."]
    selected, limitations, seen = [], [], set()
    try:
        if not directory.resolve().is_relative_to(root):
            return [], ["Runbook directory escapes the repository; ignored."]
        # Do not follow directory symlinks or scan an unbounded documentation tree.
        paths = []
        for current, directories, files in os.walk(directory, followlinks=False):
            directories[:] = sorted(d for d in directories if not (Path(current) / d).is_symlink())
            for name in sorted(files):
                if name.lower().endswith(".md"):
                    paths.append(Path(current) / name)
                    if len(paths) >= 100:
                        break
            if len(paths) >= 100:
                limitations.append("Runbook discovery limited to 100 Markdown files.")
                break
        for path in sorted(paths):
            relative = path.relative_to(root).as_posix()
            try:
                if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                    raise ValueError()
                with path.open("rb") as stream:
                    raw = stream.read(MAX_RUNBOOK_BYTES + 1)
                if len(raw) > MAX_RUNBOOK_BYTES:
                    raise ValueError()
                text = raw.decode("utf-8")
                lines = text.splitlines()
                if not lines or lines[0] != "---":
                    raise ValueError()
                end = lines.index("---", 1)
                metadata = Metadata.model_validate(yaml.safe_load("\n".join(lines[1:end])))
                if metadata.id in seen:
                    raise ValueError()
                seen.add(metadata.id)
                headers = [(i, re.sub(r"^##\s+", "", line).strip())
                           for i, line in enumerate(lines) if i > end and re.match(r"^##\s+", line)]
                if not REQUIRED_SECTIONS <= {heading.lower() for _, heading in headers}:
                    raise ValueError()
                sections = []
                for index, (start, heading) in enumerate(headers):
                    stop = headers[index + 1][0] if index + 1 < len(headers) else len(lines)
                    content = "\n".join(lines[start + 1:stop]).strip()
                    if not content:
                        raise ValueError()
                    sections.append(RunbookSection(heading=heading, line_start=start + 1,
                                                   line_end=stop, content=content))
                service_matches = len(set(services) & set(metadata.services))
                error_matches = len(set(error_classes) & set(metadata.error_classes))
                if not service_matches and "*" not in metadata.services:
                    continue
                if metadata.error_classes and not error_matches:
                    continue
                item = RunbookSnapshot(**metadata.model_dump(), file_path=relative, revision=revision,
                                       content_hash=hashlib.sha256(raw).hexdigest(), sections=sections)
                selected.append((-service_matches, -error_matches, relative,
                                 redact_evidence(item.model_dump(mode="json"), settings)))
            except (OSError, UnicodeError, ValueError, RuntimeError, ValidationError, yaml.YAMLError):
                limitations.append(f"Invalid or unreadable runbook ignored: {relative}")
    except (OSError, RuntimeError):
        limitations.append("Runbook directory could not be read.")
    selected.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in selected[:3]], limitations[:10]
