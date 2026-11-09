import httpx

from incident_copilot.observability import trace_headers
from incident_copilot.observability.runtime import model_request


class OpenAICompatibleClient:
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    async def complete(
        self,
        prompt: str,
        system_prompt: str | None = None,
        *,
        response_schema: dict | None = None,
    ) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt or ""},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        }
        if response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "incident_diagnosis",
                    "strict": True,
                    "schema": response_schema,
                },
            }
        with model_request(prompt, system_prompt, self.model, base_url=self.base_url,
                           api_key=self.api_key, response_schema=response_schema) as observation:
            headers.update(trace_headers())
            async with httpx.AsyncClient(timeout=315) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions", json=payload, headers=headers
                )
                response.raise_for_status()
                data = response.json()
                text = data["choices"][0]["message"]["content"]
                observation.set(gateway_call_id=response.headers.get("x-litellm-call-id"))
                observation.content(output=text)
                return text
