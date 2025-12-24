from __future__ import annotations

import httpx

from ..base import LLMResponse


class OpenAICompatProvider:
    id = "openai_compat"
    display_name = "OpenAI-compatible server"

    def __init__(self, base_url: str = "http://127.0.0.1:8080", api_key: str = "") -> None:
        self._base_url = base_url
        self._api_key = api_key

    def configure(self, base_url: str, api_key: str = "") -> None:
        self._base_url = base_url
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        if self._api_key.strip():
            h["Authorization"] = f"Bearer {self._api_key.strip()}"
        return h

    async def is_healthy(self) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.get(self._base_url.rstrip("/") + "/v1/models", headers=self._headers())
            if r.status_code in (200, 401, 403, 404):
                # 404 just means no listing endpoint, not necessarily a bad thing
                return True, "" if r.status_code != 404 else "No /v1/models endpoint (404)."
            return False, f"HTTP {r.status_code}"
        except Exception as e:
            return False, str(e)

    async def list_models(self) -> tuple[list[str], str]:
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(self._base_url.rstrip("/") + "/v1/models", headers=self._headers())
            if r.status_code == 404:
                return [], "Server does not expose /v1/models (404)."
            r.raise_for_status()
            data = r.json()
            out: list[str] = []
            for m in (data.get("data") or []):
                mid = (m.get("id") or "").strip()
                if mid:
                    out.append(mid)
            return sorted(set(out)), ""
        except Exception as e:
            return [], str(e)

    async def generate(self, prompt: str, model: str, temperature: float = 0.7) -> LLMResponse:
        try:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": float(temperature),
            }
            async with httpx.AsyncClient(timeout=60.0) as c:
                r = await c.post(self._base_url.rstrip("/") + "/v1/chat/completions", json=payload, headers=self._headers())
                r.raise_for_status()
                data = r.json()
            txt = ""
            try:
                txt = data["choices"][0]["message"]["content"]
            except Exception:
                txt = str(data)
            return LLMResponse(text=txt, raw=data)
        except Exception as e:
            return LLMResponse(text=f"[Compat error] {e}", raw=None)
