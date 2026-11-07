"""Route independent runtime retrieval while retaining source verification guards."""

from incident_copilot.analysis.evidence_tools import EvidenceTools
from incident_copilot.schemas.investigation import ToolOutcome


class RuntimeAwareTools:
    def __init__(self, runtime, settings, snapshot):
        self.snapshot, self.client, self.source = snapshot, runtime.runtime_client, None
        self.reason = "source_snapshot_unverified"
        if snapshot.get("revision"):
            try:
                self.source = EvidenceTools(
                    runtime.repo_root, runtime.index_db_path, settings, snapshot
                )
                self.source.validate_source()
            except ValueError:
                self.source = None

    @property
    def graph_symbol_ids(self):
        return self.source.graph_symbol_ids if self.source else []

    def restore_graph_symbols(self, symbols):
        if self.source:
            self.source.restore_graph_symbols(symbols)

    def validate_source(self):
        if self.source:
            try:
                self.source.validate_source()
            except ValueError as error:
                self.reason = str(error)
                self.source = None

    async def execute(self, request, request_id):
        if request.tool == "get_runtime_evidence":
            try:
                return await self.client.execute(request, request_id)
            except ValueError as error:
                from incident_copilot.llm.validation import validation_failure

                reason = (
                    str(error)
                    if str(error) in {"service_outside_runtime_scope", "unobserved_trace_id"}
                    else "invalid_evidence_request"
                )
                diagnostic = validation_failure(
                    stage="evidence_retrieval",
                    kind="semantic",
                    code="invalid_evidence_request",
                    path=["trace_id" if reason == "unobserved_trace_id" else "service"],
                    request_id=request_id,
                ).diagnostic
                return ToolOutcome(
                    request_id=request_id,
                    tool=request.tool,
                    status="invalid",
                    reason=reason,
                    validation_error=diagnostic,
                )
        self.validate_source()
        if self.source:
            return await self.source.execute(request, request_id)
        return ToolOutcome(
            request_id=request_id, tool=request.tool, status="unavailable", reason=self.reason
        )
