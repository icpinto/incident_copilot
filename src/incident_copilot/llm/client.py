from incident_copilot.core.settings import settings

from .adapters.ollama import OllamaClient
from .adapters.openai_compatible import OpenAICompatibleClient


def get_raw_client(runtime_settings=None):
    config = runtime_settings or settings
    from incident_copilot.observability.runtime import initialize
    telemetry = initialize()
    if telemetry:
        from incident_copilot.analysis.evidence_tools import _configured_secrets
        telemetry.secrets.update(_configured_secrets(config))
    provider = config.llm_provider.lower()
    if provider == "ollama":
        return OllamaClient(config.ollama_base_url, config.ollama_model)
    return OpenAICompatibleClient(
        config.openai_compatible_base_url,
        config.openai_compatible_api_key,
        config.openai_compatible_model,
    )


def get_client(runtime_settings=None, *, guardrail_context=None):
    config = runtime_settings or settings
    raw = get_raw_client(config)
    if config.guardrails_mode == "off" and guardrail_context is None:
        return raw
    from .guardrails import GuardedClient, GuardrailContext

    return GuardedClient(raw, guardrail_context or GuardrailContext(config))
