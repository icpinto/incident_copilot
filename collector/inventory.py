"""Report Compose containers and image provenance using the local Docker socket."""

import http.client
import json
import os
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
        containers.append({"project": project, "service": service, "container_id": container["Id"],
            "release": {"revision": labels.get("org.opencontainers.image.revision"),
                        "version": labels.get("org.opencontainers.image.version"),
                        "source_url": labels.get("org.opencontainers.image.source"),
                        "image_ref": container.get("Image"), "image_id": image_id,
                        "repo_digests": images.get(image_id, [])}})
    return {"collector_id": os.environ.get("IC_COLLECTOR_ID", "legacy"),
            "projects": {name: sorted(services) for name, services in projects.items()}, "containers": containers}


if __name__ == "__main__":
    print(json.dumps(inventory()))
