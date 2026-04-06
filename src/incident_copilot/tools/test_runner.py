import subprocess
from pathlib import Path

from incident_copilot.schemas.validation import ValidationResult


def run_command(repo_root: Path, command: list[str]) -> ValidationResult:
    proc = subprocess.run(command, cwd=repo_root, capture_output=True, text=True)
    return ValidationResult(
        commands_run=[" ".join(command)],
        passed=proc.returncode == 0,
        output=(proc.stdout or "") + "\n" + (proc.stderr or ""),
    )
