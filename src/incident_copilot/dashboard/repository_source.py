"""Repository identities and local-only snapshot resolution. No network operations."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit


def normalize_remote(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ValueError("Use a valid HTTPS github.com owner/repository URL.") from None
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+/?", parsed.path)
    ):
        raise ValueError(
            "Use an HTTPS github.com owner/repository URL without credentials or query parameters."
        )
    path = parsed.path.rstrip("/").removesuffix(".git")
    if path.split("/")[-1] in {".", "..", ""}:
        raise ValueError("Use a GitHub repository URL.")
    return "https://github.com" + path + ".git"


def validate_branch(value: str) -> None:
    # Equivalent to a single refs/heads/<name> ref, never an option or refspec.
    if (
        not value
        or len(value) > 250
        or value.startswith(("-", "/"))
        or value.endswith(("/", "."))
        or ".." in value
        or "@{" in value
        or value == "@"
        or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", value)
        or any(
            not part or part.startswith(".") or part.endswith(".lock") for part in value.split("/")
        )
    ):
        raise ValueError("Provide a valid tracked branch name.")


def validate_compose_path(value: str | None) -> None:
    if value and (
        PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).drive
        or ".." in value.replace("\\", "/").split("/")
        or "\\" in value
        or "\0" in value
    ):
        raise ValueError("GitHub Compose paths must be relative and stay inside the repository.")


def source_identity(profile: dict) -> str:
    fields = {
        key: profile.get(key)
        for key in ("source_type", "repo_root", "remote_url", "branch", "compose_path")
    }
    fields["source_type"] = fields["source_type"] or "local"
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:24]


def repository_root(profile: dict) -> str | None:
    if profile.get("source_type", "local") == "local":
        return profile["repo_root"]
    snapshot = profile.get("snapshot") or {}
    return (
        snapshot.get("root")
        if snapshot.get("source_identity") == source_identity(profile)
        else None
    )


def inside(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Repository path escapes managed storage.")
    return resolved


def snapshot_compose(root: Path, value: str | None) -> Path | None:
    validate_compose_path(value)
    return inside(root, root / value) if value else None


def public_snapshot_payload(value, root: str, remote: str):
    """Expose source provenance instead of managed checkout and index paths."""
    if isinstance(value, dict):
        return {
            key: "" if key == "db_path" else public_snapshot_payload(item, root, remote)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [public_snapshot_payload(item, root, remote) for item in value]
    if isinstance(value, str):
        return value.replace(root, remote)
    return value
