# Prompts

The diagnosis graph constrains the LLM to evidence-backed hypotheses.

Both adapters receive the same JSON schema: Ollama through `format`, and
OpenAI-compatible endpoints through `response_format` with `json_schema` and
`strict: true`. The prompt also includes the schema. Endpoints must support schema
output to provide LLM-assisted diagnosis.

Responses contain zero to three hypotheses (at least one for a provisional diagnosis),
with all required fields, numeric
confidence between zero and one, and unique consecutive ranks starting at one.
Ranks are sorted before reporting. Malformed JSON, missing fields, extra fields,
and invalid types trigger deterministic diagnosis.

Reports include an optional `fallback_reason`: `no_candidates`,
`llm_request_failed`, or `llm_invalid_response`. Request and validation failures
are logged without exception text or model output, which can contain secrets.

Both single-pass diagnosis and iterative investigation include typed `validation_errors`
in their reports. Each entry identifies the stage, failure kind, model call or evidence
request, and field paths with stable error codes and safe explanations. The dashboard
shows these errors above the incident views, and JSON/Markdown exports preserve them.
Rejected values, unknown field names, and raw exception context are not included.

See [structured reasoning, selective review and promotion gates](structured-reasoning.md) for the optional reasoning policies.
