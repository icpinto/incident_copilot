"""Print one Compose inventory record using the local Docker Unix socket."""

import http.client
import json
import socket


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect("/var/run/docker.sock")


connection = UnixConnection("docker")
connection.request("GET", "/containers/json?all=0")
response = connection.getresponse()
if response.status != 200:
    raise SystemExit(f"Docker inventory failed: HTTP {response.status}")
projects = {}
for container in json.load(response):
    labels = container.get("Labels") or {}
    project = labels.get("com.docker.compose.project")
    service = labels.get("com.docker.compose.service")
    if project and service:
        projects.setdefault(project, set()).add(service)
print(json.dumps({"projects": {name: sorted(services) for name, services in projects.items()}}))
