from incident_copilot.core.settings import settings

from .adapters.ollama import OllamaClient
from .adapters.openai_compatible import OpenAICompatibleClient


def get_client():
    provider = settings.llm_provider.lower()
    if provider == "ollama":
        return OllamaClient(settings.ollama_base_url, settings.ollama_model)
    return OpenAICompatibleClient(
        settings.openai_compatible_base_url,
        settings.openai_compatible_api_key,
        settings.openai_compatible_model,
    )
