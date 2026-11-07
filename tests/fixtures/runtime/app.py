"""Disposable failure generator; never used by the collector or production API."""

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = "healthy"
COUNT = 0
ERRORS = 0
DURATIONS = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        global COUNT, ERRORS, MODE
        if self.path.startswith("/scenario/"):
            MODE = self.path.rsplit("/", 1)[-1]
            self.send_response(200)
            self.end_headers()
            return
        if self.path == "/metrics":
            self.send_response(200)
            self.end_headers()
            buckets = "\n".join(
                f'http_request_duration_seconds_bucket{{le="{boundary}"}} {sum(value <= boundary for value in DURATIONS)}'
                for boundary in [0.01, 0.1, 1, 5]
            )
            self.wfile.write(
                (
                    f'http_requests_total {COUNT}\nhttp_errors_total {ERRORS}\n{buckets}\nhttp_request_duration_seconds_bucket{{le="+Inf"}} {COUNT}\nhttp_request_duration_seconds_count {COUNT}\n'
                ).encode()
            )
            return
        if self.path == "/health":
            self.send_response(503 if MODE == "unhealthy" else 200)
            self.end_headers()
            return
        if MODE == "oom":
            print(json.dumps({"message": "ResourceError: allocating fixture memory"}), flush=True)
            memory = []
            while True:
                memory.append(bytearray(8 * 1024 * 1024))
        if MODE == "slow":
            time.sleep(2)
        trace = self.headers.get("X-Trace-ID") or uuid.uuid4().hex
        start = time.time_ns()
        error = None
        try:
            if MODE == "application":
                raise ValueError("Controlled application validation failure")
            if os.environ.get("DEPENDENCY_URL"):
                request = urllib.request.Request(
                    os.environ["DEPENDENCY_URL"] + "/query", headers={"X-Trace-ID": trace}
                )
                urllib.request.urlopen(request, timeout=1).close()
            if MODE == "unhealthy":
                raise ConnectionError("Controlled database unavailable")
        except (ValueError, OSError, urllib.error.URLError) as failure:
            error = type(failure).__name__ + ": " + str(failure)
            ERRORS += 1
        finish = time.time_ns()
        COUNT += 1
        DURATIONS.append((finish - start) / 1e9)
        print(
            json.dumps(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "service": os.environ["ROLE"],
                    "trace_id": trace,
                    "duration_ms": (finish - start) / 1e6,
                    "message": error or "request completed",
                    "level": "error" if error else "info",
                }
            ),
            flush=True,
        )
        if os.environ.get("TEMPO_URL"):
            data = {
                "resourceSpans": [
                    {
                        "resource": {
                            "attributes": [
                                {
                                    "key": "service.name",
                                    "value": {"stringValue": os.environ["ROLE"]},
                                }
                            ]
                        },
                        "scopeSpans": [
                            {
                                "spans": [
                                    {
                                        "traceId": trace,
                                        "spanId": uuid.uuid4().hex[:16],
                                        "name": "fixture request",
                                        "startTimeUnixNano": str(start),
                                        "endTimeUnixNano": str(finish),
                                        "status": {"code": 2 if error else 1},
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
            try:
                urllib.request.urlopen(
                    urllib.request.Request(
                        os.environ["TEMPO_URL"],
                        data=json.dumps(data).encode(),
                        headers={"Content-Type": "application/json"},
                    ),
                    timeout=1,
                ).close()
            except OSError:
                pass
        self.send_response(500 if error else 200)
        self.end_headers()
        self.wfile.write((error or "ok").encode())


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
