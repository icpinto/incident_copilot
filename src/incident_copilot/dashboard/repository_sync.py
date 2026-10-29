"""User-requested GitHub synchronization. Importing this module never accesses GitHub."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from incident_copilot.analysis.graph_manager import ensure_graph_ready
from incident_copilot.core.settings import Settings
from incident_copilot.indexing.repo_indexer import index_repository
from incident_copilot.indexing.storage import IndexStorage
from incident_copilot.tools.git_local import clean_revision
from incident_copilot.workflows.activity_heartbeat import reasoning_heartbeat

from .activities import activity, validate_repository
from .config import control_path
from .repository_source import inside, normalize_remote, snapshot_compose, validate_branch
from .store import ControlStore, utcnow


class SyncFailure(Exception):
    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def index_fingerprint() -> str:
    versions = {}
    for package in (
        "tree-sitter",
        "tree-sitter-python",
        "tree-sitter-javascript",
        "tree-sitter-typescript",
        "tree-sitter-go",
        "tree-sitter-java",
        "tree-sitter-json",
        "tree-sitter-yaml",
    ):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    values = {
        "format": 1,
        "native": os.environ.get("IC_TREE_SITTER_ENABLED", "true").lower(),
        "versions": versions,
    }
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:24]


class GitClient:
    """Never expose Git's stderr or put a credential in argv, URLs, or .git/config."""

    def __init__(self, token: str = ""):
        self.token = token

    def run(self, *args: str, cwd: Path | None = None, timeout: int = 600) -> str:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        helper = Path(__file__).resolve().parents[1] / "tools" / "git_askpass.py"
        env.update(
            GIT_TERMINAL_PROMPT="0",
            GCM_INTERACTIVE="Never",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_LFS_SKIP_SMUDGE="1",
            IC_GITHUB_TOKEN=self.token,
            GIT_ASKPASS=f'"{Path(sys.executable).as_posix()}" "{helper.as_posix()}"',
        )
        command = [
            "git",
            "--no-optional-locks",
            "-c",
            "credential.helper=",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.longpaths=true",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.https.allow=always",
            "-c",
            "http.followRedirects=false",
        ]
        if cwd:
            command += ["-c", f"safe.directory={cwd.resolve().as_posix()}", "-C", str(cwd)]
        try:
            result = subprocess.run(
                command + list(args),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise SyncFailure(
                "GitHub operation timed out. Check connectivity and retry.", transient=True
            ) from None
        except OSError:
            raise SyncFailure("Git is unavailable on the worker. Install Git and retry.") from None
        if result.returncode:
            error = result.stderr.lower()
            if any(
                word in error
                for word in (
                    "authentication",
                    "could not read username",
                    "403",
                    "401",
                    "repository not found",
                )
            ):
                raise SyncFailure(
                    "GitHub access failed. Check the repository URL and token permissions."
                )
            if "couldn't find remote ref" in error:
                raise SyncFailure("The tracked branch does not exist on GitHub.")
            transient = any(
                word in error
                for word in (
                    "timed out",
                    "could not resolve",
                    "failed to connect",
                    "connection reset",
                    "502",
                    "503",
                    "504",
                    "429",
                )
            )
            raise SyncFailure(
                "Git operation failed. Check GitHub connectivity, repository access, and worker storage.",
                transient=transient,
            )
        return result.stdout.strip()


def valid_snapshot(snapshot: dict, cache: Path, identity: str, revision: str | None = None) -> bool:
    try:
        if (
            snapshot.get("source_identity") != identity
            or snapshot.get("index_fingerprint") != index_fingerprint()
        ):
            return False
        root = inside(cache, Path(snapshot["root"]))
        database = inside(cache, Path(snapshot["index_db_path"]))
        expected = revision or snapshot["revision"]
        if snapshot["revision"] != expected or clean_revision(root) != expected:
            return False
        stored_root, stored_revision = IndexStorage(database, read_only=True).snapshot_identity()
        return Path(stored_root).resolve() == root and stored_revision == expected
    except (KeyError, ValueError, OSError, sqlite3.Error):
        return False


def prepare_snapshot(job: dict, config: Settings, git: GitClient, progress) -> tuple[dict, bool]:
    source = job["source"]
    remote = normalize_remote(source["remote_url"])
    branch = source["branch"]
    validate_branch(branch)
    cache = config.repository_cache_dir.resolve()
    profile_dir = inside(cache, cache / job["repository_id"] / job["source_identity"])
    profile_dir.mkdir(parents=True, exist_ok=True)
    progress("checking")
    output = git.run("ls-remote", "--heads", remote, f"refs/heads/{branch}", timeout=30)
    matching = [
        line.split()[0]
        for line in output.splitlines()
        if line.split()[-1] == f"refs/heads/{branch}"
    ]
    if not matching:
        raise SyncFailure("The tracked branch does not exist on GitHub.")
    revision = matching[0]
    store = ControlStore(control_path())
    store.record_repository_check(job["id"], revision)
    profile = store.get("repositories", job["repository_id"]) or {}
    previous = profile.get("snapshot") or {}
    if valid_snapshot(previous, cache, job["source_identity"], revision):
        return previous, True
    for manifest in profile_dir.glob("*/snapshot.json"):
        try:
            cached = json.loads(manifest.read_text(encoding="utf-8"))
            if valid_snapshot(cached, cache, job["source_identity"], revision):
                return cached, False
        except (ValueError, OSError):
            continue
    progress("fetching")
    objects = inside(cache, profile_dir / "objects.git")
    if not objects.exists():
        objects.mkdir()
        git.run("init", "--bare", str(objects))
    git.run(
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        remote,
        f"+refs/heads/{branch}:refs/heads/tracked",
        cwd=objects,
    )
    revision = git.run("rev-parse", "refs/heads/tracked", cwd=objects)
    if not re.fullmatch(r"[a-f0-9]{40,64}", revision):
        raise SyncFailure("Git returned an invalid repository revision.")
    # Unique attempt directories never overwrite an already published checkout/index.
    destination = inside(cache, profile_dir / uuid4().hex)
    destination.mkdir(parents=True, exist_ok=True)
    root = inside(cache, destination / "source")
    database = destination / "index.db"
    manifest = destination / "snapshot.json"
    git.run("worktree", "add", "--detach", str(root), revision, cwd=objects)
    progress("indexing")
    compose = snapshot_compose(root, source.get("compose_path"))
    validation = validate_repository(str(root), str(compose) if compose else None)
    if not validation["valid"]:
        raise SyncFailure(validation["message"])
    summary = index_repository(root, compose, database)
    if summary.repo_revision != revision or clean_revision(root) != revision:
        raise SyncFailure("The downloaded checkout changed during indexing. Retry sync.")
    snapshot = {
        "root": str(root),
        "revision": revision,
        "index_db_path": str(database),
        "source_identity": job["source_identity"],
        "index_fingerprint": index_fingerprint(),
        "compose_path": source.get("compose_path"),
        "summary": summary.model_dump(mode="json"),
        "validation": validation,
    }
    temporary = manifest.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot), encoding="utf-8")
    temporary.replace(manifest)
    return snapshot, False


def incident_index(inputs: dict, destination: Path) -> dict:
    """Reuse a compatible prepared SQLite snapshot; all paths are pinned at submission."""
    root = Path(inputs["repo_root"])
    snapshot = inputs["repository_snapshot"]
    if clean_revision(root) != snapshot["revision"]:
        raise ValueError(
            "The incident's cached source snapshot is unavailable or changed. Sync again for new analyses."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if valid_snapshot(snapshot, Path(inputs["repository_cache_dir"]), snapshot["source_identity"]):
        with sqlite3.connect(
            Path(snapshot["index_db_path"]).resolve().as_uri() + "?mode=ro", uri=True
        ) as source_db:
            with sqlite3.connect(destination) as target_db:
                source_db.backup(target_db)
        return snapshot["summary"] | {"db_path": str(destination)}
    summary = index_repository(
        root, snapshot_compose(root, inputs.get("compose_path")), destination
    )
    if summary.repo_revision != snapshot["revision"]:
        raise ValueError("Source snapshot changed during indexing.")
    return summary.model_dump(mode="json")


@activity.defn
async def sync_repository_activity(job_id: str) -> None:
    store = ControlStore(control_path())
    job = store.get("repository_syncs", job_id)
    if not job or job["status"] == "completed":
        return
    config = Settings(**store.get("revisions", job["config_revision"]))

    def progress(stage: str):
        store.update(
            "repository_syncs",
            job_id,
            {
                "status": "running",
                "stage": stage,
                "updated_at": utcnow(),
                "error": None,
                "connection_error": None,
            },
        )

    try:
        async with reasoning_heartbeat(activity):
            snapshot, unchanged = await asyncio.to_thread(
                prepare_snapshot,
                job,
                config,
                GitClient(store.repository_token(job["repository_id"])),
                progress,
            )
            graph = job.get("graph_preparation")
            if not unchanged and config.code_graph_enabled and config.code_graph_mode == "managed":
                progress("preparing_graph")
                graph = (
                    await ensure_graph_ready(snapshot["root"], snapshot["revision"], config)
                ).model_dump(mode="json")
            store.publish_repository_sync(job_id, snapshot, unchanged=unchanged, graph=graph)
    except Exception as error:
        try:
            attempt = activity.info().attempt
        except RuntimeError:
            attempt = 1
        transient = isinstance(error, SyncFailure) and error.transient
        message = (
            str(error)
            if isinstance(error, (SyncFailure, ValueError))
            else "Repository preparation failed. Check worker storage and retry sync."
        )
        # Never persist arbitrary exception messages, source content, or Git stderr.
        if not isinstance(error, SyncFailure):
            message = "Repository preparation failed or its configuration changed. Check the profile and worker storage, then retry sync."
        retry = transient and attempt < 3
        store.update(
            "repository_syncs",
            job_id,
            {
                "status": "running" if retry else "failed",
                "stage": "retrying" if retry else "failed",
                "error": message,
                "updated_at": utcnow(),
                "completed_at": None if retry else utcnow(),
            },
        )
        from temporalio.exceptions import ApplicationError

        raise ApplicationError(message, non_retryable=not retry) from None
