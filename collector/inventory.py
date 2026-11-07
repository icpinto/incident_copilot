"""Report Compose containers and image provenance using the local Docker socket."""

import http.client
import json
import os
import re
import socket


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect("/var/run/docker.sock")


def docker_get(path):
    connection = UnixConnection("docker")
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"Docker inventory failed: HTTP {response.status}")
        return json.load(response)
    finally:
        connection.close()


ENV_NAMES = {"ENV", "ENVIRONMENT", "APP_ENV", "NODE_ENV", "FLASK_ENV", "RAILS_ENV", "ASPNETCORE_ENVIRONMENT"}
BOOLEAN = {"true", "false", "1", "0", "yes", "no", "on", "off"}


def safe_config_value(name, value):
    lower = value.lower()
    if name in ENV_NAMES:
        return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", value))
    if name == "LOG_LEVEL":
        return lower in {"trace", "debug", "info", "warn", "warning", "error", "fatal", "critical"}
    if name == "DEBUG" or name.startswith(("FEATURE_", "FF_")):
        return lower in BOOLEAN
    if name in {"PORT", "WORKERS"}:
        return bool(re.fullmatch(r"[0-9]{1,5}", value))
    if name == "HOST":
        return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,127}", value))
    return False


def runtime_config(details):
    """Capture bounded, non-secret Docker settings; never read mounted files."""
    environment = []
    raw_env = (details.get("Config") or {}).get("Env") or []
    for entry in raw_env[:128]:
        if not isinstance(entry, str) or "=" not in entry:
            continue
        name, value = entry.split("=", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name):
            continue
        visible = value if safe_config_value(name, value) else "[redacted]"
        environment.append({"name": name, "value": visible})
    mounts = []
    raw_mounts = details.get("Mounts") or []
    for mount in raw_mounts[:64]:
        destination = mount.get("Destination")
        if not isinstance(destination, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]{0,511}", destination):
            continue
        mounts.append({"destination": destination, "type": mount.get("Type") if mount.get("Type") in {"bind", "volume", "tmpfs"} else "other",
                       "read_only": not bool(mount.get("RW", True))})
    return {"status": "available", "environment": environment, "mounts": mounts,
            "truncated": len(raw_env) > 128 or len(raw_mounts) > 64}


def inventory(fetch=docker_get):
    projects = {}
    containers = []
    images = {}
    for container in fetch("/containers/json?all=0")[:2000]:
        labels = container.get("Labels") or {}
        project = labels.get("com.docker.compose.project")
        service = labels.get("com.docker.compose.service")
        if not project or not service:
            continue
        projects.setdefault(project, set()).add(service)
        image_id = container.get("ImageID")
        if image_id and image_id not in images:
            try:
                images[image_id] = fetch(f"/images/{image_id}/json").get("RepoDigests") or []
            except (OSError, RuntimeError, ValueError):
                images[image_id] = []
        try:
            configuration = runtime_config(fetch(f"/containers/{container['Id']}/json"))
        except (OSError, RuntimeError, ValueError):
            configuration = {"status": "unavailable", "environment": [], "mounts": [], "truncated": False}
        containers.append({"project": project, "service": service, "container_id": container["Id"],
            "release": {"revision": labels.get("org.opencontainers.image.revision"),
                        "version": labels.get("org.opencontainers.image.version"),
                        "source_url": labels.get("org.opencontainers.image.source"),
                        "image_ref": container.get("Image"), "image_id": image_id,
                        "repo_digests": images.get(image_id, [])}, "runtime_config": configuration})
    return {"collector_id": os.environ.get("IC_COLLECTOR_ID", "legacy"),
            "projects": {name: sorted(services) for name, services in projects.items()}, "containers": containers}


if __name__ == "__main__":
    print(json.dumps(inventory()))
