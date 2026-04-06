import httpx


class OllamaClient:
    def __init__(self, base_url: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def complete(self, prompt: str, system_prompt: str | None = None) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt if system_prompt is None else f"{system_prompt}\n\n{prompt}",
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"{self.base_url}/api/generate", json=payload)
            response.raise_for_status()
            data = response.json()
            return data.get("response", "")
