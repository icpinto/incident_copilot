"""Optional, best-effort investigation tracing. No SDK imports when disabled."""

from .runtime import (
    attach_context,
    call_scope,
    current_reference,
    initialize,
    investigation,
    new_trace,
    observed,
    shutdown,
    span,
    temporal_interceptors,
    trace_headers,
)

__all__ = [
    "attach_context", "call_scope", "current_reference", "initialize", "investigation",
    "new_trace", "observed", "shutdown", "span", "temporal_interceptors", "trace_headers",
]
