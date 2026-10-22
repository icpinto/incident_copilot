# Prompts

The diagnosis graph constrains the LLM to evidence-backed hypotheses.

Both adapters receive the same JSON schema: Ollama through `format`, and
OpenAI-compatible endpoints through `response_format` with `json_schema` and
`strict: true`. The prompt also includes the schema. Endpoints must support schema
output to provide LLM-assisted diagnosis.

Responses must contain one to three hypotheses with all required fields, numeric
confidence between zero and one, and unique consecutive ranks starting at one.
Ranks are sorted before reporting. Malformed JSON, missing fields, extra fields,
and invalid types trigger deterministic diagnosis.

Reports include an optional `fallback_reason`: `no_candidates`,
`llm_request_failed`, or `llm_invalid_response`. Request and validation failures
are logged without exception text or model output, which can contain secrets.
