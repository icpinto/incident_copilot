"""Persist collector identity before Vector expands its environment variables."""

import os
import re
import sys
from pathlib import Path
from uuid import uuid4


def collector_identity(directory: Path) -> str:
    override = os.environ.get("IC_COLLECTOR_ID")
    if override:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", override):
            raise RuntimeError("IC_COLLECTOR_ID must be a unique identifier using letters, digits, dots, colons, underscores, or hyphens.")
        return override
    directory.mkdir(parents=True, exist_ok=True)
    identity_file = directory / "collector-id"
    try:
        with identity_file.open("x", encoding="utf-8") as output:
            identity = str(uuid4())
            output.write(identity)
    except FileExistsError:
        identity = identity_file.read_text(encoding="utf-8").strip()
    if not identity or len(identity) > 128:
        raise RuntimeError("Invalid collector identity; set IC_COLLECTOR_ID explicitly.")
    return identity


if __name__ == "__main__":
    os.environ["IC_COLLECTOR_ID"] = collector_identity(Path("/var/lib/vector"))
    os.execvp("vector", ["vector", *(sys.argv[1:] or ["--config", "/etc/vector/vector.yaml"])])
