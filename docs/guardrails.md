# Model policy checks

Install the optional SDK with `pip install -e ".[guardrails]"`. The application
Docker image includes NeMo Guardrails 0.24.1 and its packaged policy files.
Single-pass diagnosis and iterative investigation share the same policy layer.

In Configuration, choose **Guardrails mode: Observe** for new analyses. Alternatively,
set `IC_GUARDRAILS_MODE=observe` and recreate the API and worker. Saved dashboard
overrides take precedence over environment values. Existing incident settings
revisions remain unchanged. The built-in and Compose default is `off`.

Modes:

- `off`: no SDK initialization, safety model calls, or policy inspection.
- `observe`: record findings and unavailable checks; continue with the original
  diagnosis prompt and JSON response. Existing redaction still applies.
- `enforce`: stop the affected model step on rejection or unavailable inspection.
  Retain earlier accepted findings, or return deterministic insufficient evidence
  when no assessment was accepted.

Input checks classify the untrusted logs, source, retrieval results, and prior model
text actually included in each prompt. Application instructions and response schemas
are excluded. The classifier uses the current model and provider through an unguarded
internal client, with no recursive policy calls. It receives a redacted copy; its
verdict does not rewrite the diagnosis input. Output checks detect configured
credentials and recognizable secret assignments, credential URLs, and private keys
locally, including inside JSON request arguments. Redaction markers and common
placeholders are allowed. These checks are not a comprehensive secret scanner.

NeMo runs packaged custom input/output rails through `LLMRails.check_async()`.
Only fixed markers enter the NeMo runtime; actions inspect the per-analysis content
in memory. Reports, journal verdicts, and policy diagnostics contain reason codes,
timings, identifiers, and counters. Raw classifier explanations, exception bodies,
and matching secrets are never included in policy diagnostics. Observation mode can
still accept flagged model content, so existing report redaction remains important.

Each investigation permits eight checks, separate from its four assessment attempts.
Successful input inspection adds one model call per distinct input; output inspection
is local. `IC_GUARDRAILS_TIMEOUT_SECONDS` defaults to 10 seconds per check and all checks
share the original investigation deadline. Input and output inspection limits are
32 KiB and 64 KiB respectively; oversized content is an inspection failure. Checks
do not retry automatically. Reservations and completed verdicts survive worker retries,
with cache identity tied to content, policy version, and relevant settings. A changed
configuration on a resumed execution is unavailable inspection, not a cached pass.

The incident Guardrails panel and JSON/Markdown exports show mode, policy version,
events, elapsed check time, and additional model calls. Policy findings are separate
from analysis validation errors. Historical reports remain readable without this field.
In enforcement mode, reasons include `guardrail_input_blocked`,
`guardrail_output_blocked`, and `guardrail_check_unavailable`.

Before enabling enforcement, review representative incidents and adversarial fixtures
in observation mode. Evaluate injection detection, benign source and security-log
false positives, credential patterns, unavailable checks, and latency. Model-based
classification is probabilistic; a passing verdict does not establish safety or
diagnosis correctness. Citation validation, source restrictions, and retrieval budgets
continue to run independently. Grounding and general content moderation are deferred.
