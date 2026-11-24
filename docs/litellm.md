# LiteLLM Gateway

LiteLLM runs as the default LLM gateway in `docker-compose.dev.yml`. Both single-pass diagnosis and
iterative investigation use the existing OpenAI-compatible HTTP adapter:

```text
Diagnosis / investigation -> OpenAI-compatible adapter -> LiteLLM -> provider
```

No LiteLLM Python dependency is installed in the API or worker. The default stack
routes to Ollama through the gateway. The gateway image is pinned by version and digest
in `docker-compose.dev.yml`; `LITELLM_IMAGE` can override it for an upgrade.

## Start with Ollama

1. Run Ollama on the Docker host and pull a model, for example
   `ollama pull llama3.1:8b`. Ollama must accept connections from Docker containers.
2. Generate a gateway key with Python:

   ```bash
   python -c "import secrets; print('sk-' + secrets.token_hex(32))"
   ```

   Copy `.env.example` to `.env` if needed. Put the generated value in the project's
   private `.env` file, before the application credential reference:

   ```env
   LITELLM_MASTER_KEY=sk-<generated-value>
   LITELLM_MODEL=ollama_chat/llama3.1:8b
   LITELLM_API_BASE=http://host.docker.internal:11434
   LITELLM_API_KEY=ollama
   IC_LLM_PROVIDER=openai_compatible
   IC_OPENAI_COMPATIBLE_BASE_URL=http://litellm:4000/v1
   IC_OPENAI_COMPATIBLE_MODEL=incident-diagnosis
   IC_OPENAI_COMPATIBLE_API_KEY=${LITELLM_MASTER_KEY}
   ```

   The upstream API key is a placeholder for local Ollama. The gateway key is
   required: Compose rejects the main stack when it is missing or empty.
3. Start the stack:

   ```bash
   docker compose -f docker-compose.dev.yml up --build -d
   ```

The main Compose file defaults `IC_LLM_PROVIDER` to `openai_compatible`, the endpoint to
`http://litellm:4000/v1`, the model to `incident-diagnosis`, and the application key
to `LITELLM_MASTER_KEY` for both API and worker. They wait for the gateway process
to become healthy. The gateway port is published only on `127.0.0.1:4000`; the
upstream model need not be reachable for the process health check to pass.

Open **Configuration** at http://localhost:8080. Existing `.env` files must use the
application values above to switch from direct Ollama. Saved dashboard overrides have
priority over environment values. If you previously saved provider settings,
reset **Provider**, **OpenAI-compatible endpoint**, **OpenAI-compatible model**,
and **API key**, then save to use the default gateway configuration. Alternatively,
explicitly save those gateway values. Model settings apply to new analyses;
existing runs retain their settings revisions.

Use the **Language model** connection check to verify the gateway's authenticated
`/v1/models` endpoint. It checks reachability and credentials, not inference or
schema support. Submit an analysis with source candidates and inspect its engine
and fallback reason to verify an actual model call. A working gateway returns
`deterministic+llm`; `llm_request_failed` or `llm_invalid_response` indicates a
provider, request, or response problem. View gateway logs with:

```bash
docker compose -f docker-compose.dev.yml logs --tail=100 litellm
```

## Change providers

The public model alias remains `incident-diagnosis`. Change the upstream in `.env`,
then recreate the gateway with the main Compose file. For an OpenAI-compatible
upstream, including a hosted provider or local inference server:

```env
LITELLM_MODEL=openai/<upstream-model-id>
LITELLM_API_BASE=https://<provider-host>/v1
LITELLM_API_KEY=<provider-api-key>
```

```bash
docker compose -f docker-compose.dev.yml up -d litellm
```

For provider-specific routing, additional aliases, or fallbacks, edit the mounted
`configs/litellm.yaml` using LiteLLM's
[configuration reference](https://docs.litellm.ai/docs/proxy/configs) and
[provider documentation](https://docs.litellm.ai/docs/providers).
Pass any additional credentials explicitly to the gateway's environment. Restart
`litellm` after editing its YAML. Provider keys belong in the gateway environment;
the application only needs its gateway credential.

Diagnosis and investigation send `response_format.type=json_schema` with
`strict: true`. Every configured upstream and fallback must support the requested
schema and the adapter's `temperature: 0` parameter. The gateway preserves unsupported
parameter errors (`drop_params: false`); application-side response validation and
deterministic fallback remain active. See LiteLLM's
[structured-output support](https://docs.litellm.ai/docs/completion/json_mode).

Gateway retries default to zero, with a 300-second router timeout below both model
adapters' 315-second HTTP client timeout. Iterative investigation has a separate
180-second deadline and can cancel a wait earlier. Enhanced single-pass generation
and review share a 315-second deadline, with review capped at 60 seconds; baseline
has no additional shared wall-clock timeout wrapper. Temporal activity retries and
timeouts remain outer execution boundaries. When enabling gateway retries or model
fallbacks, account for all these limits. See the
[model request architecture](architecture/agent-harness.md#litellm-gateway-and-adapters).

## External gateway or host processes

To use an existing external gateway, override these values for both API and
worker, or save the equivalent fields in **Configuration**:

```env
IC_LLM_PROVIDER=openai_compatible
IC_OPENAI_COMPATIBLE_BASE_URL=https://<gateway-host>/v1
IC_OPENAI_COMPATIBLE_MODEL=incident-diagnosis
IC_OPENAI_COMPATIBLE_API_KEY=<gateway-key>
```

The bundled gateway remains part of the Compose stack and still requires its own
`LITELLM_MASTER_KEY`; the application uses the explicitly configured external key.

For host processes calling the bundled gateway, use `http://localhost:4000/v1`.
For containers calling a service on the host, use a host address reachable from
Docker, such as `http://host.docker.internal:4000/v1` on Docker Desktop.
The Compose service name `litellm` is reachable only inside its Docker network.

## Credentials and gateway persistence

The bundled gateway uses file configuration and master-key authentication without
a database. It does not provide virtual keys, persistent spend tracking, or budget
enforcement. Use a separately managed, database-backed gateway if you need those
features, and give the application a restricted virtual key. See the
[LiteLLM deployment guide](https://docs.litellm.ai/docs/proxy/docker_quick_start).

Gateway model configuration is mounted read-only. Prompt and response logging to
logging providers is disabled through `turn_off_message_logging`; do not enable
verbose debugging on sensitive incident evidence. Application credentials retain
the existing settings redaction and stay out of Temporal payloads.

For explicit direct-Ollama development, set `IC_LLM_PROVIDER=ollama` and configure
`IC_OLLAMA_BASE_URL` and `IC_OLLAMA_MODEL`, then reset saved dashboard provider
overrides. Recreate API and worker after changing environment values:

```bash
docker compose -f docker-compose.dev.yml up -d api worker
```

The gateway remains part of the main stack; direct Ollama is an explicit override.
