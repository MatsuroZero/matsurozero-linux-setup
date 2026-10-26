"""Small async client for Google's OpenAI-compatible Gemini API."""
from __future__ import annotations

import httpx


class GeminiProviderError(RuntimeError):
    """A safe, user-facing Gemini API error."""


class GeminiProvider:
    def __init__(self, api_key: str, *, base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai", timeout: float = 90.0):
        if not api_key or not api_key.strip():
            raise GeminiProviderError("GEMINI_API_KEY is not configured.")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout

    def _client(self):
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout, connect=8.0),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )

    async def list_models(self) -> list[str]:
        async with self._client() as client:
            response = await client.get("models")
            if response.is_error:
                raise GeminiProviderError(_safe_error(response))
            payload = response.json()
        data = payload.get("data", []) if isinstance(payload, dict) else []
        names = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("id", "")).strip()
            if name and name not in names and "embedding" not in name.casefold():
                names.append(name)
        return sorted(names, key=lambda value: (not value.startswith("gemini-"), value.casefold()))

    async def chat(self, *, model: str, messages: list[dict], max_tokens: int = 512) -> dict:
        async with self._client() as client:
            response = await client.post("chat/completions", json={
                "model": model,
                "messages": messages,
                "max_tokens": max(64, min(int(max_tokens), 4096)),
                "temperature": 0.65,
            })
            if response.is_error:
                raise GeminiProviderError(_safe_error(response))
            payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("choices"), list):
            raise GeminiProviderError("Gemini returned an unexpected response format.")
        return payload


def _safe_error(response: httpx.Response) -> str:
    """Describe API failures without echoing request headers or secrets."""
    try:
        payload = response.json()
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        message = str(error.get("message", "")) if isinstance(error, dict) else ""
    except ValueError:
        message = ""
    if response.status_code in (401, 403):
        return "Google rejected the API key or its permissions. Check GEMINI_API_KEY and API access."
    if response.status_code == 429:
        return "Google AI Studio rate limit or quota reached. Check quota and billing settings."
    return f"Google Gemini API returned HTTP {response.status_code}" + (f": {message[:300]}" if message else ".")


def model_display_name(model: str) -> str:
    return model.removeprefix("models/")
