from __future__ import annotations

import httpx

from ..base import LLMResponse


class OllamaProvider:
    id = "ollama"
    display_name = "Ollama (local)"

    def __init__(self, base_url: str = "http://127.0.0.1:11434") -> None:
        self._base_url = base_url

    def configure(self, base_url: str) -> None:
        self._base_url = base_url

    async def is_healthy(self) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.get(self._base_url.rstrip("/") + "/api/tags")
            if r.status_code >= 400:
                return False, f"HTTP {r.status_code}"
            return True, ""
        except Exception as e:
            return False, str(e)

    async def list_models(self) -> tuple[list[str], str]:
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(self._base_url.rstrip("/") + "/api/tags")
                r.raise_for_status()
                data = r.json()
            out: list[str] = []
            for m in data.get("models", []) or []:
                name = (m.get("name") or "").strip()
                if name:
                    out.append(name)
            return sorted(set(out)), ""
        except Exception as e:
            return [], str(e)

    async def generate(self, prompt: str, model: str, temperature: float = 0.7) -> LLMResponse:
        try:
            payload = {"model": model, "prompt": prompt, "stream": False, "options": {"temperature": float(temperature)}}
            async with httpx.AsyncClient(timeout=60.0) as c:
                r = await c.post(self._base_url.rstrip("/") + "/api/generate", json=payload)
                r.raise_for_status()
                data = r.json()
            return LLMResponse(text=str(data.get("response") or ""), raw=data)
        except Exception as e:
            return LLMResponse(text=f"[Ollama error] {e}", raw=None)
