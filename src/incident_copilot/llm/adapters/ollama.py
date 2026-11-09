import httpx

from incident_copilot.observability.runtime import model_request


class OllamaClient:
    def __init__(self, base_url: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def complete(
        self,
        prompt: str,
        system_prompt: str | None = None,
        *,
        response_schema: dict | None = None,
    ) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0},
        }
        if system_prompt is not None:
            payload["system"] = system_prompt
        if response_schema is not None:
            payload["format"] = response_schema
        with model_request(prompt, system_prompt, self.model, direct=True,
                           response_schema=response_schema) as observation:
            observation.set(**{"gen_ai.request.model": self.model,
                               "langfuse.observation.model.name": self.model})
            async with httpx.AsyncClient(timeout=315) as client:
                response = await client.post(f"{self.base_url}/api/generate", json=payload)
                response.raise_for_status()
                data = response.json()
                observation.set(**{"gen_ai.usage.input_tokens": data.get("prompt_eval_count"),
                                   "gen_ai.usage.output_tokens": data.get("eval_count")})
                text = data.get("response", "")
                observation.content(output=text)
                return text
