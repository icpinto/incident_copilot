"""Heartbeat reasoning while awaited model and retrieval calls are in flight."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress


@asynccontextmanager
async def reasoning_heartbeat(activity, interval_seconds: float = 5):
    """Also supports direct API/CLI calls outside a Temporal activity context."""
    def heartbeat() -> None:
        try:
            activity.heartbeat()
        except (AttributeError, RuntimeError):
            # No Temporal context when a legacy activity is invoked directly.
            pass

    async def pulse() -> None:
        while True:
            heartbeat()
            await asyncio.sleep(interval_seconds)

    heartbeat()
    task = asyncio.create_task(pulse())
    try:
        yield heartbeat
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
