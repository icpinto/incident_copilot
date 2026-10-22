from incident_copilot.core.settings import settings

from .adapters.ollama import OllamaClient
from .adapters.openai_compatible import OpenAICompatibleClient


def get_client(runtime_settings=None):
    config = runtime_settings or settings
    provider = config.llm_provider.lower()
    if provider == "ollama":
        return OllamaClient(config.ollama_base_url, config.ollama_model)
    return OpenAICompatibleClient(
        config.openai_compatible_base_url,
        config.openai_compatible_api_key,
        config.openai_compatible_model,
    )
