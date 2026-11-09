"""Verify a running LiteLLM gateway and Langfuse share one nested trace."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx


def main():
    from dotenv import dotenv_values
    values = dotenv_values(".env") | dotenv_values(".env.langfuse")
    for key, value in values.items():
        if value is not None:
            os.environ.setdefault(key, value)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", default="http://localhost:4000/v1")
    args = parser.parse_args()
    os.environ["IC_LANGFUSE_ENABLED"] = "true"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from incident_copilot.llm.adapters.openai_compatible import OpenAICompatibleClient
    from incident_copilot.observability import (
        call_scope,
        current_reference,
        initialize,
        investigation,
        shutdown,
    )
    telemetry = initialize()
    if telemetry is None:
        raise SystemExit("Configure Langfuse project credentials first; see docs/langfuse.md.")

    async def invoke():
        with investigation("smoke-" + uuid4().hex):
            reference = current_reference()
            with call_scope("smoke"):
                await OpenAICompatibleClient(args.gateway_url,
                    os.environ.get("IC_OPENAI_COMPATIBLE_API_KEY") or os.environ["LITELLM_MASTER_KEY"],
                    os.environ.get("IC_OPENAI_COMPATIBLE_MODEL", "incident-diagnosis")).complete(
                        "Reply with OK.", "You are checking local trace continuity.")
            return reference
    try:
        reference = asyncio.run(invoke())
        shutdown()
        endpoint = telemetry.settings.base_url.rstrip("/") + "/api/public/v2/observations"
        since = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        with httpx.Client(timeout=5, auth=(telemetry.settings.public_key,
                                         telemetry.settings.secret_key.get_secret_value())) as client:
            for _ in range(30):
                result = client.get(endpoint, params={"traceId": reference["trace_id"], "fromStartTime": since,
                                                     "toStartTime": datetime.now(UTC).isoformat(), "limit": 100})
                result.raise_for_status()
                observations = result.json().get("data", [])
                requests = {row["id"] for row in observations if row.get("name") == "llm.request"}
                parents = {row["id"]: row.get("parentObservationId") for row in observations}
                generations = [row for row in observations if row.get("type", "").upper() == "GENERATION"]
                for generation in generations:
                    parent = generation.get("parentObservationId")
                    visited = set()
                    while parent and parent not in visited:
                        if parent in requests:
                            print(json.dumps({"status": "passed", **reference, "generations": len(generations)}))
                            return
                        visited.add(parent)
                        parent = parents.get(parent)
                time.sleep(1)
        raise SystemExit("Trace nesting was not observed within 30 seconds; check gateway and Langfuse logs.")
    except (httpx.HTTPError, KeyError, ValueError) as error:
        raise SystemExit(f"Smoke check unavailable ({type(error).__name__}); check local services and credentials.") from None
    finally:
        shutdown()


if __name__ == "__main__":
    main()
