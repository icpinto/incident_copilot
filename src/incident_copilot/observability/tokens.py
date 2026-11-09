from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import httpx

logger = logging.getLogger(__name__)


class TokenCounter:
    """Bounded, off-path local-tokenizer accounting; never contacts a provider."""
    def __init__(self, telemetry):
        self.telemetry = telemetry
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="prompt-accounting")
        self.slots = threading.BoundedSemaphore(32)
        self.cache = OrderedDict()
        self.lock = threading.Lock()

    def submit(self, sections, model, base_url, api_key, carrier, metadata):
        if not self.slots.acquire(blocking=False):
            return
        try:
            future = self.pool.submit(self._count, sections, model, base_url, api_key, carrier, metadata)
            future.add_done_callback(lambda _: self.slots.release())
        except Exception:  # noqa: BLE001 -- Token accounting is best effort.
            self.slots.release()

    def _count(self, sections, model, base_url, api_key, carrier, metadata):
        from .runtime import span, trace_headers
        deadline = time.monotonic() + 3
        endpoint = base_url.rstrip("/").removesuffix("/v1") + "/utils/token_counter"
        records = []
        with span("prompt.composition", carrier=carrier, attributes=metadata) as observation:
            try:
                with httpx.Client(timeout=1, trust_env=False) as client:
                    for section in sections:
                        count, tokenizer = None, None
                        digest = hashlib.sha256(section.text.encode()).hexdigest()
                        key = (endpoint, model, digest)
                        with self.lock:
                            cached = self.cache.get(key)
                        if cached and time.monotonic() - cached[0] < 60:
                            _, count, tokenizer = cached
                        elif time.monotonic() < deadline:
                            try:
                                response = client.post(endpoint, params={"call_endpoint": "false"},
                                    headers={"Authorization": f"Bearer {api_key}", **trace_headers()},
                                    json={"model": model, "prompt": section.text})
                                response.raise_for_status()
                                data = response.json()
                                value = data.get("total_tokens")
                                if type(value) is int and value >= 0:
                                    count = value
                                    tokenizer = {key: data[key] for key in ("tokenizer_type", "model_used") if key in data}
                                    with self.lock:
                                        self.cache[key] = (time.monotonic(), count, tokenizer)
                                        while len(self.cache) > 512:
                                            self.cache.popitem(last=False)
                            except Exception:  # noqa: BLE001 -- Token accounting is best effort.
                                logger.debug("Section token estimate unavailable")
                        records.append({"category": section.category, "evidence_id": section.evidence_id,
                                        "kind": section.kind, "tokens_estimated": count,
                                        "tokenizer": tokenizer, "bytes": len(section.text.encode())})
            except Exception:  # noqa: BLE001 -- Token accounting is best effort.
                records = [{"category": s.category, "evidence_id": s.evidence_id,
                            "kind": s.kind, "tokens_estimated": None} for s in sections]
            observation.set(model=model, counts_are_estimates=True, sections=records,
                            count_status="complete" if all(r["tokens_estimated"] is not None for r in records) else "unavailable",
                            accounting_note="Section estimates exclude provider message overhead and may differ from usage totals.")

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)
