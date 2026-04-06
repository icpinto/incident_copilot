from __future__ import annotations

import asyncio

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

try:
    from temporalio.client import Client  # type: ignore
    from temporalio.worker import Worker  # type: ignore
except Exception as exc:  # pragma: no cover
    raise SystemExit("Temporal dependencies are not installed. Install with `.[temporal]`.") from exc


async def main() -> None:
    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
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
