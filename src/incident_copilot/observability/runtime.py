from __future__ import annotations

import atexit
import contextvars
import functools
import hashlib
import inspect
import json
import logging
import os
import secrets
import threading
from contextlib import contextmanager
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)
_metadata = contextvars.ContextVar("incident_trace_metadata", default=None)
_call = contextvars.ContextVar("incident_model_call", default=None)
_runtime = None
_initialized = False
_lock = threading.Lock()


class ObservabilitySettings(BaseSettings):
    # Separate from analysis Settings: credentials never enter revision snapshots.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    enabled: bool = Field(default=False, validation_alias="IC_LANGFUSE_ENABLED")
    capture_content: bool = Field(default=False, validation_alias="IC_LANGFUSE_CAPTURE_CONTENT")
    public_url: str = Field(default="http://localhost:3000", validation_alias="IC_LANGFUSE_PUBLIC_URL")
    base_url: str = Field(default="http://localhost:3000", validation_alias="LANGFUSE_BASE_URL")
    public_key: str = Field(default="", validation_alias="LANGFUSE_PUBLIC_KEY")
    secret_key: SecretStr = Field(default="", validation_alias="LANGFUSE_SECRET_KEY")
    project_id: str = Field(default="incident-copilot", validation_alias="LANGFUSE_PROJECT_ID")


class Observation:
    def __init__(self, native=None):
        self.native = native
        self.metadata = {}

    def set(self, **attributes):
        if self.native is None:
            return
        try:
            for key, value in attributes.items():
                if value is not None:
                    cleaned = _runtime.clean(value)
                    self.native.set_attribute(key, cleaned if isinstance(cleaned, (str, bool, int, float))
                                              else json.dumps(cleaned, ensure_ascii=False))
                    if not key.startswith(("langfuse.", "gen_ai.")):
                        self.metadata[key] = cleaned
            self.native.set_attribute("langfuse.observation.metadata", json.dumps(self.metadata, ensure_ascii=False))
        except Exception:  # noqa: BLE001 -- Observability must never interrupt analysis.
            logger.debug("Telemetry attributes unavailable")

    def content(self, *, input=None, output=None):
        if _runtime and _runtime.settings.capture_content:
            self.set(**{"langfuse.observation.input": input, "langfuse.observation.output": output})


class Telemetry:
    def __init__(self, settings):
        from langfuse import Langfuse
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        self.settings = settings
        self.secrets = {value for key, value in os.environ.items()
                        if (any(part in key.lower() for part in ("password", "secret", "api_key", "auth_token", "access_token", "session_token", "encryption_key"))
                            or key.upper().endswith("_TOKEN"))
                        and value}
        self.secrets.add(settings.secret_key.get_secret_value())
        self.provider = TracerProvider(resource=Resource.create({"service.name": "incident-copilot"}))
        self.client = Langfuse(
            public_key=settings.public_key, secret_key=settings.secret_key.get_secret_value(),
            base_url=settings.base_url, tracer_provider=self.provider, sample_rate=1.0,
            should_export_span=lambda candidate: True, mask_otel_spans=self.mask,
            timeout=2, span_exporter=_safe_exporter(settings, self.clean),
        )
        self.tracer = self.provider.get_tracer("incident_copilot")
        from .tokens import TokenCounter
        self.counter = TokenCounter(self)

    def clean(self, value):
        from incident_copilot.analysis.evidence_tools import _SECRET_ASSIGNMENT, _redact_text
        known = sorted((s for s in self.secrets.copy() if s), key=len, reverse=True)

        def redact(item):
            if isinstance(item, dict):
                return {key: "[REDACTED]" if isinstance(child, str) and _SECRET_ASSIGNMENT.match(f"{key}=")
                        else redact(child) for key, child in item.items()}
            if isinstance(item, (list, tuple)):
                cleaned = [redact(child) for child in item]
                return tuple(cleaned) if isinstance(item, tuple) else cleaned
            if isinstance(item, str):
                # SDK attributes contain JSON strings. Preserve their structure
                # while scrubbing keys and nested text at the export boundary.
                if item.lstrip().startswith(("{", "[")):
                    try:
                        parsed = json.loads(item)
                    except (ValueError, RecursionError):
                        pass
                    else:
                        if isinstance(parsed, (dict, list)):
                            return json.dumps(redact(parsed), ensure_ascii=False)
                return _redact_text(item, known)
            return item
        return redact(value)

    def mask(self, *, params):
        from langfuse.types import MaskOtelSpansResult, OtelSpanPatch
        patches = {}
        for key, candidate in params.spans.items():
            updates = {}
            removals = []
            for name, value in candidate.attributes.items():
                if name in {"exception.message", "exception.stacktrace"} or not self.settings.capture_content and (
                    name.startswith(("gen_ai.input", "gen_ai.output"))
                    or name in {"langfuse.observation.input", "langfuse.observation.output"}
                ):
                    removals.append(name)
                else:
                    cleaned = self.clean(value)
                    if cleaned != value:
                        updates[name] = cleaned
            if updates or removals:
                patches[key] = OtelSpanPatch(set_attributes=updates, delete_attributes=removals)
        return MaskOtelSpansResult(span_patches=patches)

    def reference(self, trace_id):
        return {"trace_id": trace_id,
                "trace_url": f"{self.settings.public_url.rstrip('/')}/project/{self.settings.project_id}/traces/{trace_id}"}



def _safe_exporter(settings, clean):
    """Sanitize third-party span events/status as well as SDK attribute masking."""
    import base64

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExporter
    from opentelemetry.trace import Status

    authorization = base64.b64encode(
        f"{settings.public_key}:{settings.secret_key.get_secret_value()}".encode()).decode()
    delegate = OTLPSpanExporter(
        endpoint=settings.base_url.rstrip("/") + "/api/public/otel/v1/traces",
        headers={"Authorization": "Basic " + authorization, "x-langfuse-ingestion-version": "4"},
        timeout=2,
    )

    class SafeExporter(SpanExporter):
        def export(self, spans):
            sanitized = []
            for candidate in spans:
                attributes = {}
                for key, value in (candidate.attributes or {}).items():
                    if key in {"exception.message", "exception.stacktrace"}:
                        continue
                    if not settings.capture_content and (key.startswith(("gen_ai.input", "gen_ai.output", "gen_ai.prompt", "gen_ai.completion"))
                            or key in {"langfuse.observation.input", "langfuse.observation.output"}):
                        continue
                    attributes[key] = clean(value)
                sanitized.append(ReadableSpan(
                    name=candidate.name, context=candidate.context, parent=candidate.parent,
                    resource=candidate.resource, attributes=attributes, events=(), links=candidate.links,
                    kind=candidate.kind, status=Status(candidate.status.status_code),
                    start_time=candidate.start_time, end_time=candidate.end_time,
                    instrumentation_scope=candidate.instrumentation_scope,
                ))
            return delegate.export(sanitized)

        def shutdown(self):
            delegate.shutdown()

        def force_flush(self, timeout_millis=30000):
            return delegate.force_flush(timeout_millis)
    return SafeExporter()


def initialize():
    global _runtime, _initialized
    if _initialized:
        return _runtime
    with _lock:
        if _initialized:
            return _runtime
        _initialized = True
        try:
            config = ObservabilitySettings()
            for url in (config.base_url, config.public_url):
                parsed = urlsplit(url)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
                    raise ValueError("Invalid observability endpoint")
            if config.enabled and config.public_key and config.secret_key.get_secret_value():
                _runtime = Telemetry(config)
        except Exception:  # noqa: BLE001 -- Observability must never interrupt analysis.
            # Neither validation errors nor SDK failures may disclose credential values.
            logger.warning("Langfuse initialization unavailable; analysis will continue")
        return _runtime


def shutdown():
    global _runtime, _initialized
    runtime = _runtime
    if runtime:
        try:
            runtime.counter.close()
            runtime.provider.force_flush(timeout_millis=3000)
            runtime.client.shutdown()
        except Exception:  # noqa: BLE001 -- Observability must never interrupt analysis.
            logger.warning("Langfuse flush unavailable")
    _runtime, _initialized = None, False


atexit.register(shutdown)


def _extract(carrier):
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
    return TraceContextTextMapPropagator().extract(carrier or {})


def trace_headers():
    if initialize() is None:
        return {}
    try:
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
        carrier = {}
        TraceContextTextMapPropagator().inject(carrier)
        return carrier
    except Exception:  # noqa: BLE001 -- Observability must never interrupt analysis.
        return {}


@contextmanager
def attach_context(carrier):
    if initialize() is None or not carrier:
        yield
        return
    from opentelemetry.context import attach, detach
    token = attach(_extract(carrier))
    try:
        yield
    finally:
        detach(token)


@contextmanager
def span(name, *, carrier=None, attributes=None, generation=False):
    runtime = initialize()
    native = None
    if runtime:
        try:
            native = runtime.tracer.start_span(name, context=_extract(carrier) if carrier else None)
        except Exception:  # noqa: BLE001 -- Observability must never interrupt analysis.
            logger.debug("Telemetry span unavailable")
    if native is None:
        yield Observation()
        return
    from opentelemetry.trace import Status, StatusCode, use_span
    observation = Observation(native)
    observation.set(**{"langfuse.observation.type": "generation" if generation else "span",
                       "langfuse.trace.name": "Incident analysis", **(_metadata.get() or {}), **(attributes or {})})
    try:
        with use_span(native, end_on_exit=False, record_exception=False, set_status_on_exception=False):
            yield observation
    except BaseException as error:
        observation.set(outcome="canceled" if isinstance(error, (KeyboardInterrupt, GeneratorExit))
                        or type(error).__name__ == "CancelledError" else "error",
                        error_type=type(error).__name__)
        from incident_copilot.schemas.validation import AnalysisValidationError
        diagnostic = getattr(error, "diagnostic", None)
        if isinstance(diagnostic, AnalysisValidationError):
            observation.set(validation_stage=diagnostic.stage,
                            validation_codes=[issue.code for issue in diagnostic.issues])
        native.set_status(Status(StatusCode.ERROR))
        raise
    else:
        observation.set(outcome="completed")
    finally:
        native.end()


def new_trace(execution_id):
    runtime = initialize()
    if runtime is None:
        return None, None
    trace_id = hashlib.sha256(f"incident-copilot:{execution_id}".encode()).hexdigest()[:32]
    carrier = {"traceparent": f"00-{trace_id}-{secrets.token_hex(8)}-01"}
    with span("investigation", carrier=carrier, attributes={"execution_id": execution_id}) as observation:
        if observation.native is None:
            return None, None
        return runtime.reference(trace_id), trace_headers()



def execution_metadata(config=None, **values):
    """Annotate subsequent spans without putting runtime settings in telemetry."""
    _metadata.set({**(_metadata.get() or {}), **values})
    runtime = initialize()
    if runtime and config is not None:
        from incident_copilot.analysis.evidence_tools import _configured_secrets
        runtime.secrets.update(_configured_secrets(config))


def current_execution_id():
    return (_metadata.get() or {}).get("execution_id")


def current_reference():
    runtime = initialize()
    header = trace_headers().get("traceparent", "")
    return runtime.reference(header.split("-")[1]) if runtime and header else None


@contextmanager
def investigation(execution_id, *, carrier=None, config=None, **metadata):
    runtime = initialize()
    if runtime and config is not None:
        from incident_copilot.analysis.evidence_tools import _configured_secrets
        runtime.secrets.update(_configured_secrets(config))
    token = _metadata.set({**(_metadata.get() or {}), "execution_id": execution_id, **metadata})
    try:
        # In activities prefer the interceptor's active parent. Stored context is
        # the recovery path for activities/workflows created before instrumentation.
        active = trace_headers()
        if not active and not carrier:
            _, carrier = new_trace(execution_id)
        with span("analysis", carrier=carrier if not active else None) as observation:
            yield observation
    finally:
        _metadata.reset(token)


@contextmanager
def call_scope(purpose, *, round=None, model_call=None):
    token = _call.set({"purpose": purpose, "round": round, "model_call": model_call})
    try:
        yield
    finally:
        _call.reset(token)


def observed(name):
    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def asynchronous(*args, **kwargs):
                with span(name) as observation:
                    if name == "evidence.retrieve":
                        request = args[1] if len(args) > 1 else kwargs.get("request")
                        request_id = args[2] if len(args) > 2 else kwargs.get("request_id")
                        observation.set(tool=getattr(request, "tool", None), request_id=request_id)
                    result = await function(*args, **kwargs)
                    if name == "evidence.retrieve":
                        observation.set(result_status=getattr(result, "status", None),
                                        evidence_ids=[item.id for item in getattr(result, "evidence", [])])
                    if isinstance(result, dict):
                        observation.set(**{key: result[key] for key in ("fallback_reason", "diagnosis_status", "engine", "status", "code") if key in result})
                        reasoning = result.get("reasoning") or {}
                        observation.set(review_status=reasoning.get("review_status"), review_failure=reasoning.get("review_failure"))
                    return result
            asynchronous.__signature__ = inspect.signature(function, eval_str=True)
            return asynchronous
        @functools.wraps(function)
        def synchronous(*args, **kwargs):
            with span(name):
                return function(*args, **kwargs)
        synchronous.__signature__ = inspect.signature(function, eval_str=True)
        return synchronous
    return decorate


def temporal_interceptors():
    runtime = initialize()
    if runtime is None:
        return []
    try:
        from temporalio.contrib.opentelemetry import TracingInterceptor
        return [TracingInterceptor(tracer=runtime.tracer)]
    except Exception:  # noqa: BLE001 -- Observability must never interrupt analysis.
        logger.warning("Temporal tracing unavailable; analysis will continue")
        return []


@contextmanager
def model_request(prompt, system_prompt, model, *, base_url=None, api_key=None, direct=False,
                  response_schema=None):
    runtime = initialize()
    if runtime and api_key:
        runtime.secrets.add(api_key)
    with span("llm.request", attributes={"model": model, **(_call.get() or {})}, generation=direct) as observation:
        observation.content(input={"system": system_prompt, "prompt": str(prompt),
                                   "response_schema": response_schema})
        if runtime and base_url:
            try:
                from .prompts import manifest
                runtime.counter.submit(manifest(prompt, system_prompt), model, base_url, api_key,
                                       trace_headers(), dict(_metadata.get() or {}) | dict(_call.get() or {}))
            except Exception:  # noqa: BLE001 -- Observability must never interrupt analysis.
                logger.debug("Prompt accounting unavailable")
        yield observation


def entrypoint(function):
    """Create an execution scope for direct API and CLI entry points."""
    if inspect.iscoroutinefunction(function):
        @functools.wraps(function)
        async def asynchronous(*args, **kwargs):
            with investigation(str(uuid4()), entrypoint=function.__name__):
                return await function(*args, **kwargs)
        asynchronous.__signature__ = inspect.signature(function, eval_str=True)
        return asynchronous
    @functools.wraps(function)
    def synchronous(*args, **kwargs):
        try:
            with investigation(str(uuid4()), entrypoint=function.__name__):
                return function(*args, **kwargs)
        finally:
            shutdown()
    synchronous.__signature__ = inspect.signature(function, eval_str=True)
    return synchronous


def dashboard_activity(function):
    @functools.wraps(function)
    async def wrapped(incident_id: str, stage: str):
        if initialize() is None:
            return await function(incident_id, stage)
        from incident_copilot.core.settings import Settings
        from incident_copilot.dashboard.config import control_path
        from incident_copilot.dashboard.store import ControlStore
        store = ControlStore(control_path())
        _runtime.secrets.update(store.repository_secrets())
        run = store.get("incidents", incident_id) or {}
        config = Settings(**(store.get("revisions", run.get("config_revision", "")) or {}))
        try:
            from temporalio import activity
            attempt = activity.info().attempt
        except RuntimeError:
            attempt = 1
        with investigation(incident_id, carrier=run.get("_trace_context"), config=config,
                           incident_id=incident_id, config_revision=run.get("config_revision"),
                           stage=stage, activity_attempt=attempt), span(stage):
            return await function(incident_id, stage)
    wrapped.__signature__ = inspect.signature(function, eval_str=True)
    return wrapped
