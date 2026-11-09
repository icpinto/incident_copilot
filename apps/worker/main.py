from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

if __package__ is None or __package__ == "":  # pragma: no cover
    sys.path.append(str(Path(__file__).resolve().parents[2] / "src"))

from incident_copilot.core.logging import configure_logging
from incident_copilot.dashboard.activities import (
    dashboard_stage_activity,
    validate_repository_activity,
)
from incident_copilot.dashboard.config import Configuration, control_path, fingerprint
from incident_copilot.dashboard.repository_sync import sync_repository_activity
from incident_copilot.dashboard.store import ControlStore, utcnow
from incident_copilot.workflows.temporal_activities import (
    build_service_map_and_index_activity,
    correlate_logs_activity,
    deep_analysis_activity,
    diagnosis_graph_activity,
)
from incident_copilot.workflows.temporal_workflows import (
    DiagnoseIncidentWorkflow,
    SyncRepositoryWorkflow,
    ValidateRepositoryWorkflow,
)

configure_logging()
logger = logging.getLogger(__name__)

try:
    from temporalio.client import Client  # type: ignore
    from temporalio.worker import Worker  # type: ignore
except Exception as exc:  # pragma: no cover
    raise SystemExit("Temporal dependencies are not installed. Install with `.[temporal]`.") from exc


async def main() -> None:
    from incident_copilot.observability import initialize, shutdown, temporal_interceptors
    initialize()
    store = ControlStore(control_path())
    configuration = Configuration(store)
    settings = configuration.effective()
    max_attempts = settings.temporal_connect_max_attempts
    retry_delay_seconds = settings.temporal_connect_retry_delay_seconds
    for attempt in range(1, max_attempts + 1):
        try:
            client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace,
                                          interceptors=temporal_interceptors())
            break
        except RuntimeError:
            if attempt == max_attempts:
                raise
            logger.warning(
                "Failed to connect to Temporal at %s (attempt %s/%s). Retrying in %ss.",
                settings.temporal_host,
                attempt,
                max_attempts,
                retry_delay_seconds,
            )
            await asyncio.sleep(retry_delay_seconds)

    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[DiagnoseIncidentWorkflow, ValidateRepositoryWorkflow, SyncRepositoryWorkflow],
        activities=[
            build_service_map_and_index_activity,
            correlate_logs_activity,
            deep_analysis_activity,
            diagnosis_graph_activity,
            dashboard_stage_activity,
            validate_repository_activity,
            sync_repository_activity,
        ],
    )
    from uuid import uuid4
    worker_id = str(uuid4())

    async def heartbeat():
        while True:
            store.put("workers", worker_id, {
                "updated_at": utcnow(), "infrastructure": fingerprint(configuration.infrastructure),
            })
            await asyncio.sleep(5)

    pulse = asyncio.create_task(heartbeat())
    try:
        await worker.run()
    finally:
        pulse.cancel()
        from contextlib import suppress
        with suppress(asyncio.CancelledError):
            await pulse
        await asyncio.to_thread(shutdown)


if __name__ == "__main__":
    asyncio.run(main())
