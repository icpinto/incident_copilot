"""Per-analysis NeMo checks. Policy engines and raw content never enter checkpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from importlib.resources import files

from incident_copilot.analysis.evidence_tools import contains_secret, redact_evidence
from incident_copilot.schemas.guardrails import GuardrailEvent, GuardrailSummary

MAX_CHECKS = 8
INPUT_BYTES = 32768
OUTPUT_BYTES = 65536
_POLICIES = files("incident_copilot.llm").joinpath("policies")
_INJECTION_PROMPT = _POLICIES.joinpath("injection.txt").read_text(encoding="utf-8")
POLICY_VERSION = (
    "incident-v1-"
    + hashlib.sha256(
        (
            "nemoguardrails==0.24.1;secret-detector-v1;"
            + _INJECTION_PROMPT
            + _POLICIES.joinpath("config.yml").read_text(encoding="utf-8")
            + _POLICIES.joinpath("rails.co").read_text(encoding="utf-8")
        ).encode()
    ).hexdigest()[:16]
)
_CLASSIFIER_SCHEMA = {
    "type": "object",
    "properties": {"injection": {"type": "boolean"}},
    "required": ["injection"],
    "additionalProperties": False,
}


class GuardrailFailure(Exception):
    """Only fixed reason codes may leave the policy layer."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class GuardrailBlocked(GuardrailFailure):
    pass


class GuardrailUnavailable(GuardrailFailure):
    pass


class _InspectionLimit(Exception):
    pass


class GuardrailContext:
    def __init__(self, settings, *, journal=None, deadline: float | None = None):
        self.settings = settings
        self.journal = journal
        self.deadline = deadline
        self.input_text: str | None = None
        self.model_call = 0
        self.on_update: Callable[[dict], Awaitable[None]] | None = None
        # Include credential rotations in cache identity without persisting credentials.
        relevant = {
            key: value
            for key, value in settings.model_dump(mode="json").items()
            if key.startswith(("llm_", "ollama_", "openai_compatible_", "guardrails_"))
            or "secret" in key
            or "auth_token" in key
        }
        # Historical baseline checkpoints predate the reasoning-mode setting.
        if settings.llm_reasoning_mode == "baseline":
            relevant.pop("llm_reasoning_mode", None)
        self._fingerprint = hashlib.sha256(
            json.dumps(relevant, sort_keys=True).encode()
        ).hexdigest()
        saved = journal.get("guardrails") if journal else None
        self._summary = (
            GuardrailSummary.model_validate(saved["summary"])
            if saved
            else GuardrailSummary(mode=settings.guardrails_mode, policy_version=POLICY_VERSION)
        )
        for event in self._summary.events:
            if event.status == "pending":
                event.status, event.code = "skipped", "check_interrupted"
        self._results = saved.get("results", {}) if saved else {}
        self._mismatch = bool(
            saved
            and (
                saved["fingerprint"] != self._fingerprint
                or self._summary.policy_version != POLICY_VERSION
            )
        )
        self._saved_fingerprint = saved["fingerprint"] if saved else self._fingerprint
        self._engine = None
        self._active_text = ""
        self._action_result: tuple[str, str] | None = None
        self._raw = None
        self._persist()

    def summary(self) -> dict:
        return self._summary.model_dump(mode="json")

    def _persist(self):
        if self.journal:
            self.journal.put(
                "guardrails",
                {
                    "fingerprint": self._saved_fingerprint,
                    "summary": self.summary(),
                    "results": self._results,
                },
            )

    async def _record(self, event: GuardrailEvent, *, complete=True):
        events = {item.id: item for item in self._summary.events}
        events[event.id] = event
        self._summary.events = list(events.values())
        self._summary.duration_ms = sum(item.duration_ms for item in events.values())
        if complete:
            self._results[event.id] = event.model_dump(mode="json")
        self._persist()
        if self.on_update:
            await self.on_update(self.summary())

    def _enforce(self, event: GuardrailEvent):
        if self._summary.mode != "enforce":
            return
        if event.status == "flagged":
            raise GuardrailBlocked(f"guardrail_{event.stage}_blocked")
        if event.status != "passed":
            raise GuardrailUnavailable("guardrail_check_unavailable")

    def _load_engine(self):
        # Lazy imports keep the core application usable without the optional SDK.
        from nemoguardrails import LLMRails, RailsConfig
        from nemoguardrails.actions import action
        from nemoguardrails.actions.rail_outcome import RailOutcome

        @action(is_system_action=True)
        async def incident_check_input():
            try:
                self._summary.model_calls += 1
                self._persist()  # Reserve before contacting the external model.
                text = await self._raw.complete(
                    prompt=self._active_text,
                    system_prompt=_INJECTION_PROMPT,
                    response_schema=_CLASSIFIER_SCHEMA,
                )
                if not isinstance(text, str) or len(text.encode()) > 1024:
                    raise ValueError("classifier_invalid_response")
                verdict = json.loads(text)
                if (
                    not isinstance(verdict, dict)
                    or set(verdict) != {"injection"}
                    or type(verdict["injection"]) is not bool
                ):
                    raise ValueError("classifier_invalid_response")
            except (ValueError, TypeError):
                self._action_result = ("error", "classifier_invalid_response")
                return RailOutcome.failure(reason="classifier_invalid_response")
            except Exception:  # noqa: BLE001 -- Never expose provider exceptions to NeMo logs.
                self._action_result = ("error", "classifier_request_failed")
                return RailOutcome.failure(reason="classifier_request_failed")
            self._action_result = (
                ("flagged", "prompt_injection") if verdict["injection"] else ("passed", "passed")
            )
            return RailOutcome.block() if verdict["injection"] else RailOutcome.allow()

        @action(is_system_action=True)
        async def incident_check_output():
            found = contains_secret(self._active_text, self.settings)
            self._action_result = (
                ("flagged", "secret_disclosure") if found else ("passed", "passed")
            )
            return RailOutcome.block() if found else RailOutcome.allow()

        config = RailsConfig.from_content(
            yaml_content=_POLICIES.joinpath("config.yml").read_text(encoding="utf-8"),
            colang_content=_POLICIES.joinpath("rails.co").read_text(encoding="utf-8"),
        )
        engine = LLMRails(config, verbose=False)
        engine.register_action(incident_check_input, name="incident_check_input")
        engine.register_action(incident_check_output, name="incident_check_output")
        return engine

    async def check(self, stage: str, text: str, raw):
        identity = hashlib.sha256(
            (POLICY_VERSION + self._fingerprint + stage + text).encode()
        ).hexdigest()
        event_id = "gr_" + identity[:24]
        if event_id in self._results and not self._mismatch:
            self._enforce(GuardrailEvent.model_validate(self._results[event_id]))
            return
        event = GuardrailEvent(
            id=event_id,
            stage=stage,
            model_call=self.model_call,
            status="pending",
            code="check_pending",
        )
        if self._summary.checks >= MAX_CHECKS:
            event.status, event.code = "skipped", "check_limit"
            await self._record(event)
            self._enforce(event)
            return
        self._summary.checks += 1
        await self._record(event, complete=False)  # Durable reservation, even on cancellation.
        started = time.monotonic()
        try:
            if self._mismatch:
                event.status, event.code = "error", "configuration_changed"
            elif len(text.encode()) > (INPUT_BYTES if stage == "input" else OUTPUT_BYTES):
                event.status, event.code = "error", "inspection_limit"
            else:
                timeout = self.settings.guardrails_timeout_seconds
                if self.deadline is not None:
                    timeout = min(timeout, self.deadline - time.time())
                if timeout <= 0:
                    raise TimeoutError
                async with asyncio.timeout(timeout):
                    self._raw = raw
                    if stage == "input":
                        try:
                            data = json.loads(text)
                        except ValueError:
                            data = text
                        redacted = redact_evidence(data, self.settings)
                        self._active_text = (
                            redacted
                            if isinstance(redacted, str)
                            else json.dumps(redacted, ensure_ascii=False)
                        )
                    else:
                        self._active_text = text
                    if len(self._active_text.encode()) > (
                        INPUT_BYTES if stage == "input" else OUTPUT_BYTES
                    ):
                        raise _InspectionLimit
                    if self._engine is None:
                        self._engine = await asyncio.to_thread(self._load_engine)
                    self._action_result = None
                    # Content stays in action closures. SDK traces see only a fixed marker,
                    # never source, model output, matching credentials, or classifier text.
                    role = "user" if stage == "input" else "assistant"
                    result = await self._engine.check_async(
                        messages=[{"role": role, "content": "Incident policy check."}],
                    )
                    if self._action_result is None or result.status.value == "modified":
                        event.status, event.code = "error", "check_execution_failed"
                    else:
                        event.status, event.code = self._action_result
                        expected = "blocked" if event.status in {"flagged", "error"} else "passed"
                        if result.status.value.lower() != expected:
                            event.status, event.code = "error", "check_execution_failed"
        except ImportError:
            event.status, event.code = "error", "dependency_unavailable"
        except _InspectionLimit:
            event.status, event.code = "error", "inspection_limit"
        except TimeoutError:
            event.status, event.code = "error", "check_timeout"
        except Exception:  # noqa: BLE001 -- Policy exceptions can contain untrusted input.
            event.status, event.code = "error", "check_execution_failed"
        finally:
            self._active_text = ""
            self._raw = None
        event.duration_ms = round((time.monotonic() - started) * 1000, 2)
        await self._record(event)
        self._enforce(event)


class GuardedClient:
    def __init__(self, raw, context: GuardrailContext):
        self.raw = raw
        self.context = context

    async def complete(
        self, prompt: str, system_prompt: str | None = None, *, response_schema: dict | None = None
    ) -> str:
        await self.context.check(
            "input",
            self.context.input_text if self.context.input_text is not None else prompt,
            self.raw,
        )
        text = await self.raw.complete(
            prompt=prompt, system_prompt=system_prompt, response_schema=response_schema
        )
        if not isinstance(text, str):
            # Keep malformed provider responses out of the SDK; caller validation still runs.
            await self.context.check("output", " " * (OUTPUT_BYTES + 1), self.raw)
        else:
            await self.context.check("output", text, self.raw)
        return text
