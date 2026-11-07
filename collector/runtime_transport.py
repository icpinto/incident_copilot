"""Strictly bounded HTTP GETs. No shell, redirects, proxies, or Docker writes."""

import http.client
import json
import re
import socket
import ssl
import time
from urllib.parse import urlsplit

MAX_READ = 1024 * 1024
DOCKER_PATH = re.compile(
    r"^/(?:containers/json|containers/[A-Za-z0-9_.-]+/(?:json|logs|stats))(?:\?[^\r\n]*)?$"
)


class ReadFailure(Exception):
    pass


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")


def read_response(connection, method, path, *, headers=None, body=None, deadline=None):
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        if response.status < 200 or response.status >= 300:
            raise ReadFailure("http_" + str(response.status))
        if response.getheader("Content-Encoding", "identity") != "identity":
            raise ReadFailure("unsupported_content_encoding")
        chunks, size = [], 0
        while True:
            if deadline is not None and time.time() >= deadline:
                raise ReadFailure("request_deadline")
            if deadline is not None and connection.sock is not None:
                connection.sock.settimeout(max(0.01, min(5, deadline - time.time())))
            chunk = response.read1(min(65536, MAX_READ + 1 - size))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_READ:
                raise ReadFailure("upstream_response_limit")
    except (OSError, http.client.HTTPException) as error:
        raise ReadFailure("transport_unavailable") from error
    finally:
        connection.close()


def docker_get(path, *, deadline=None, raw=False):
    if not DOCKER_PATH.fullmatch(path):
        raise ReadFailure("docker_operation_forbidden")
    timeout = min(5, max(0.01, deadline - time.time())) if deadline else 5
    content = read_response(
        UnixConnection("docker", timeout=timeout), "GET", path, deadline=deadline
    )
    return content if raw else json.loads(content)


def http_request(base, path, *, token="", body=None, deadline=None):
    endpoint = urlsplit(base)
    if (
        endpoint.scheme not in {"http", "https"}
        or not endpoint.hostname
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
        or "\r" in base
        or "\n" in base
    ):
        raise ReadFailure("invalid_backend_url")
    if not path.startswith("/") or path.startswith("//"):
        raise ReadFailure("invalid_backend_path")
    timeout = min(5, max(0.01, deadline - time.time())) if deadline else 5
    cls = http.client.HTTPSConnection if endpoint.scheme == "https" else http.client.HTTPConnection
    options = {"context": ssl.create_default_context()} if endpoint.scheme == "https" else {}
    connection = cls(endpoint.hostname, endpoint.port, timeout=timeout, **options)
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    if token:
        headers["Authorization"] = "Bearer " + token
    content = None
    if body is not None:
        content = json.dumps(body, allow_nan=False).encode()
        headers["Content-Type"] = "application/json"
    return json.loads(
        read_response(
            connection,
            "POST" if body is not None else "GET",
            endpoint.path.rstrip("/") + path,
            headers=headers,
            body=content,
            deadline=deadline,
        )
    )
