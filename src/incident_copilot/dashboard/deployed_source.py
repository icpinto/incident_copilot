"""Prepare exact declared commits without advancing repository profiles."""

import json
import re
from pathlib import Path
from uuid import uuid4

from incident_copilot.indexing.repo_indexer import index_repository
from incident_copilot.tools.git_local import clean_revision

from .activities import validate_repository
from .repository_source import inside, normalize_remote, snapshot_compose, source_identity
from .repository_sync import GitClient, SyncFailure, index_fingerprint, valid_snapshot


def prepare_deployed_snapshot(inputs: dict, config, token: str = "") -> dict:
    deployment = inputs.get("deployment_context") or {}
    revision = (deployment.get("revision") or "").strip().lower()
    if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", revision):
        raise SyncFailure("Missing or invalid deployed commit. Set org.opencontainers.image.revision to the full Git SHA, rebuild, and recreate the container.")
    source = inputs["repository_profile"]
    identity = inputs["source_identity"]
    if source_identity(source) != identity:
        raise SyncFailure("The captured repository identity is invalid. Submit a new analysis.")
    local = source["source_type"] == "local"
    remote = source["repo_root"] if local else normalize_remote(source["remote_url"])
    if not local and deployment.get("source_url"):
        try:
            declared_remote = normalize_remote(deployment["source_url"])
        except ValueError:
            raise SyncFailure("The image source label must identify the configured GitHub repository using an HTTPS URL.") from None
        if declared_remote != remote:
            raise SyncFailure("The image source label differs from the configured repository. Correct the service mapping or image labels.")
    cache = Path(inputs["repository_cache_dir"]).resolve()
    profile_dir = inside(cache, cache / source["id"] / identity)
    profile_dir.mkdir(parents=True, exist_ok=True)
    pinned = inputs.get("repository_snapshot")
    if pinned and valid_snapshot(pinned, cache, identity, revision):
        return pinned
    # Manual branch sync and deployed-version analyses share validated snapshots.
    cached_source = None
    for manifest in profile_dir.glob("*/snapshot.json"):
        try:
            candidate = json.loads(manifest.read_text(encoding="utf-8"))
            if valid_snapshot(candidate, cache, identity, revision):
                return candidate
            candidate_root = inside(cache, Path(candidate["root"]))
            if candidate.get("source_identity") == identity and clean_revision(candidate_root) == revision:
                cached_source = str(candidate_root)
        except (OSError, ValueError, KeyError):
            continue
    # Each attempt owns its object database and checkout. Concurrent requests and
    # manual sync cannot move each other's refs or publish half-built indexes.
    destination = inside(cache, profile_dir / uuid4().hex)
    destination.mkdir()
    objects = destination / "objects.git"
    root = destination / "source"
    database = destination / "index.db"
    git = GitClient(token, allow_local=local or cached_source is not None)
    origin = cached_source or remote
    if local and not cached_source:
        origin_path = Path(origin)
        if not origin_path.is_absolute() or not origin_path.is_dir():
            raise SyncFailure("The configured local Git repository is unavailable on the worker.")
        top = git.run("rev-parse", "--show-toplevel", cwd=origin_path, timeout=10)
        if Path(top).resolve() != origin_path.resolve():
            raise SyncFailure("Exact-version analysis requires a local profile pointing to the Git repository root.")
    object_format = "sha256" if len(revision) == 64 else "sha1"
    git.run("init", "--bare", "--object-format=" + object_format, str(objects), timeout=10)
    try:
        git.run("fetch", "--no-tags", "--no-recurse-submodules", "--", origin, revision, cwd=objects, timeout=120)
        actual = git.run("rev-parse", "--verify", revision + "^{commit}", cwd=objects, timeout=10)
    except SyncFailure as error:
        if error.transient:
            raise
        raise SyncFailure("The declared commit is unavailable in the configured repository. Check that the commit was pushed, the local history contains it, and repository credentials are valid; then rerun.") from None
    if actual != revision:
        raise SyncFailure("The fetched object does not match the declared commit.")
    git.run("worktree", "add", "--detach", str(root), revision, cwd=objects, timeout=60)
    compose = source.get("compose_path")
    if local and compose and Path(compose).is_absolute():
        try:
            compose = Path(compose).resolve().relative_to(Path(source["repo_root"]).resolve()).as_posix()
        except ValueError:
            raise SyncFailure("Version-linked Compose files must be inside the configured repository.") from None
    compose_path = snapshot_compose(root, compose)
    validation = validate_repository(str(root), str(compose_path) if compose_path else None)
    if not validation["valid"]:
        raise SyncFailure(validation["message"])
    summary = index_repository(root, compose_path, database)
    if summary.repo_revision != revision or clean_revision(root) != revision:
        raise SyncFailure("The prepared source changed during indexing. Retry the investigation.")
    snapshot = {"root": str(root), "revision": revision, "index_db_path": str(database),
                "source_identity": identity, "index_fingerprint": index_fingerprint(),
                "compose_path": compose, "summary": summary.model_dump(mode="json"), "validation": validation}
    temporary = destination / "snapshot.tmp"
    temporary.write_text(json.dumps(snapshot), encoding="utf-8")
    temporary.replace(destination / "snapshot.json")
    return snapshot
