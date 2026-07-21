from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from ..base import LLMResponse


class OpenAICompatProvider:
    id = "openai_compat"
    display_name = "OpenAI-compatible server"

    def __init__(self, base_url: str = "http://127.0.0.1:8080", api_key: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client: httpx.AsyncClient | None = None

    def configure(self, base_url: str, api_key: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=3.0),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5, keepalive_expiry=30.0),
                trust_env=False,
            )
        return self._client

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None and not client.is_closed:
            await client.aclose()

    def _headers(self) -> dict[str, str]:
        if self._api_key.strip():
            return {"Authorization": f"Bearer {self._api_key.strip()}"}
        return {}

    def _url(self, path: str) -> str:
        return self._base_url + path

    async def is_healthy(self) -> tuple[bool, str]:
        try:
            response = await self._get_client().get(self._url("/v1/models"), headers=self._headers(), timeout=3.0)
            if response.status_code in (200, 401, 403, 404):
                return True, "" if response.status_code != 404 else "No /v1/models endpoint (404)."
            return False, f"HTTP {response.status_code}"
        except Exception as exc:
            return False, str(exc)

    async def list_models(self) -> tuple[list[str], str]:
        try:
            response = await self._get_client().get(self._url("/v1/models"), headers=self._headers(), timeout=8.0)
            if response.status_code == 404:
                return [], "Server does not expose /v1/models (404)."
            response.raise_for_status()
            data = response.json()
            models = [str(item.get("id") or "").strip() for item in data.get("data", []) or []]
            return sorted({model for model in models if model}), ""
        except Exception as exc:
            return [], str(exc)

    async def generate(self, prompt: str, model: str, temperature: float = 0.7) -> LLMResponse:
        try:
            payload = self._payload(prompt, model, temperature, stream=False)
            response = await self._get_client().post(
                self._url("/v1/chat/completions"),
                json=payload,
                headers=self._headers(),
            )
            response.raise_for_status()
            data = response.json()
            try:
                text = data["choices"][0]["message"]["content"]
            except Exception:
                text = str(data)
            return LLMResponse(text=str(text or ""), raw=data)
        except Exception as exc:
            return LLMResponse(text=f"[Compat error] {exc}", raw=None)

    async def stream_generate(self, prompt: str, model: str, temperature: float = 0.7) -> AsyncIterator[str]:
        payload = self._payload(prompt, model, temperature, stream=True)
        try:
            async with self._get_client().stream(
                "POST",
                self._url("/v1/chat/completions"),
                json=payload,
                headers=self._headers(),
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if line == "[DONE]":
                        return
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    try:
                        choice = data["choices"][0]
                        content = (choice.get("delta") or {}).get("content")
                        if content is None:
                            content = (choice.get("message") or {}).get("content")
                        if content is None:
                            content = choice.get("text")
                    except Exception:
                        content = None
                    if content:
                        yield str(content)
        except Exception as exc:
            yield f"[Compat error] {exc}"

    def _payload(prompt: str, model: str, temperature: float, stream: bool) -> dict[str, object]:
        return {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": float(temperature),
            "stream": stream,
        }
