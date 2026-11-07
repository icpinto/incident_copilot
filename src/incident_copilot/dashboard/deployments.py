"""Container observations and immutable per-incident deployment context."""

import hashlib
import json

from incident_copilot.schemas.deployment import DeploymentContext, ReleaseMetadata, RuntimeConfiguration

SCHEMA = """
CREATE TABLE IF NOT EXISTS collector_sources (
    collector_id TEXT NOT NULL, project TEXT NOT NULL,
    services TEXT NOT NULL DEFAULT '[]', repository_id TEXT,
    enabled INTEGER NOT NULL DEFAULT 0, last_seen TEXT NOT NULL,
    environment TEXT NOT NULL DEFAULT 'unknown', versioned_services TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(collector_id,project));
CREATE TABLE IF NOT EXISTS deployments (
    id TEXT PRIMARY KEY, collector_id TEXT NOT NULL, container_id TEXT NOT NULL,
    project TEXT NOT NULL, service TEXT NOT NULL, payload TEXT NOT NULL,
    UNIQUE(collector_id,container_id));
CREATE INDEX IF NOT EXISTS deployment_observed ON deployments(json_extract(payload,'$.last_observed'));
"""


def deployment_key(collector_id: str, container_id: str) -> str:
    return hashlib.sha256(json.dumps([collector_id, container_id]).encode()).hexdigest()


def observe(db, *, collector_id: str, project: str, service: str, container_id: str,
            release: ReleaseMetadata, now: str, runtime_config: RuntimeConfiguration | None = None) -> dict:
    key = deployment_key(collector_id, container_id)
    row = db.execute("SELECT payload FROM deployments WHERE id=?", (key,)).fetchone()
    value = json.loads(row[0]) if row else {
        "deployment_id": key, "collector_id": collector_id, "project": project,
        "service": service, "container_id": container_id,
        "first_observed": now, **release.model_dump(),
    }
    if value["project"] != project or value["service"] != service:
        raise ValueError("Container identity conflicts with its previous observation.")
    # Enrich missing information only. Buffered events carry their own revision;
    # an inventory refresh cannot substitute a different release declaration.
    for name, item in release.model_dump().items():
        if not value.get(name) and item:
            value[name] = item
    if runtime_config and (not value.get("runtime_config") or
                           value["runtime_config"].get("status") != "available" and runtime_config.status == "available"):
        value["runtime_config"] = runtime_config.model_dump()
    if "release_identity" not in value:
        value["release_identity"] = hashlib.sha256(json.dumps([
            value.get("revision"), value.get("image_id") or value.get("repo_digests") or container_id
        ]).encode()).hexdigest()
    value["last_observed"] = max(now, value.get("last_observed", now))
    db.execute("""INSERT INTO deployments VALUES (?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET payload=excluded.payload""",
        (key, collector_id, container_id, project, service, json.dumps(value)))
    return value


def context(observation: dict, source: dict, release: ReleaseMetadata | None = None) -> dict:
    value = observation | {"environment": source.get("environment") or "unknown"}
    if release is not None:
        # The batch's declaration is authoritative, including a missing revision.
        # Image identity may be enriched from the exact same container observation.
        for name in ("revision", "version", "source_url", "image_ref"):
            value[name] = getattr(release, name)
    return DeploymentContext.model_validate(value).model_dump()


def get_deployment(db, deployment_id: str) -> dict | None:
    row = db.execute("SELECT payload FROM deployments WHERE id=?", (deployment_id,)).fetchone()
    return json.loads(row[0]) if row else None
