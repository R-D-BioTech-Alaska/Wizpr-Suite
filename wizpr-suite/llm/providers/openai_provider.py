from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from ..base import LLMResponse


class OpenAIProvider:
    id = "openai"
    display_name = "OpenAI"

    def __init__(self, api_key: str = "", base_url: str = "") -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._client: OpenAI | None = None

    def configure(self, api_key: str, base_url: str = "") -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._client = None

    def _get_client(self) -> OpenAI:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise RuntimeError("OpenAI API key is not set.")
        kwargs: dict[str, Any] = {"api_key": self._api_key}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        self._client = OpenAI(**kwargs)
        return self._client

    async def is_healthy(self) -> tuple[bool, str]:
        try:
            await asyncio.to_thread(self._get_client)
            return True, ""
        except Exception as e:
            return False, str(e)

    async def list_models(self) -> tuple[list[str], str]:
        try:
            client = self._get_client()
            resp = await asyncio.to_thread(client.models.list)
            ids: list[str] = []
            for m in getattr(resp, "data", []) or []:
                mid = getattr(m, "id", "") or ""
                if mid:
                    ids.append(str(mid))
            return sorted(set(ids)), ""
        except Exception as e:
            return [], str(e)

    async def generate(self, prompt: str, model: str, temperature: float = 0.7) -> LLMResponse:
        try:
            client = self._get_client()
            def _call():
                return client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=float(temperature),
                )
            resp = await asyncio.to_thread(_call)
            txt = ""
            try:
                txt = resp.choices[0].message.content or ""
            except Exception:
                txt = str(resp)
            return LLMResponse(text=txt, raw=resp)
        except Exception as e:
            return LLMResponse(text=f"[OpenAI error] {e}", raw=None)
