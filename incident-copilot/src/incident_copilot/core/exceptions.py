class IncidentCopilotError(Exception):
    """Base error."""


class IndexingError(IncidentCopilotError):
    """Repo indexing failed."""


class CorrelationError(IncidentCopilotError):
    """Log correlation failed."""


class LLMError(IncidentCopilotError):
    """LLM interaction failed."""
