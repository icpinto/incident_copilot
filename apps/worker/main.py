from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

if __package__ is None or __package__ == "":  # pragma: no cover
    sys.path.append(str(Path(__file__).resolve().parents[2] / "src"))

from incident_copilot.core.logging import configure_logging
from incident_copilot.core.settings import settings
from incident_copilot.workflows.temporal_activities import (
    build_service_map_and_index_activity,
    correlate_logs_activity,
    deep_analysis_activity,
    diagnosis_graph_activity,
)
from incident_copilot.workflows.temporal_workflows import DiagnoseIncidentWorkflow

configure_logging()
logger = logging.getLogger(__name__)

try:
    from temporalio.client import Client  # type: ignore
    from temporalio.worker import Worker  # type: ignore
except Exception as exc:  # pragma: no cover
    raise SystemExit("Temporal dependencies are not installed. Install with `.[temporal]`.") from exc


async def main() -> None:
    max_attempts = 10
    retry_delay_seconds = 2
    for attempt in range(1, max_attempts + 1):
        try:
            client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
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
        workflows=[DiagnoseIncidentWorkflow],
        activities=[
            build_service_map_and_index_activity,
            correlate_logs_activity,
            deep_analysis_activity,
            diagnosis_graph_activity,
        ],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
