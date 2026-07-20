from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from ..base import LLMResponse


class OpenAIProvider:
    id = "openai"
    display_name = "OpenAI"
    transcription_prompt = "Vocabulary: WIZPR Ring, Wizpr, Codex, OpenCode, Ollama, Qwen."

    def __init__(self, api_key: str = "", base_url: str = "") -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._client: Any | None = None
        self._async_client: Any | None = None
        self._retired_clients: list[Any] = []

    def configure(self, api_key: str, base_url: str = "") -> None:
        if api_key == self._api_key and base_url == self._base_url:
            return
        self._api_key = api_key
        self._base_url = base_url
        if self._client is not None:
            self._retired_clients.append(self._client)
        if self._async_client is not None:
            self._retired_clients.append(self._async_client)
        self._client = None
        self._async_client = None

    def _client_kwargs(self) -> dict[str, Any]:
        if not self._api_key:
            raise RuntimeError("OpenAI API key is not set.")
        kwargs: dict[str, Any] = {
            "api_key": self._api_key,
            "max_retries": 1,
            "timeout": 120.0,
        }
        if self._base_url:
            kwargs["base_url"] = self._base_url
        return kwargs

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        from openai import OpenAI

        self._client = OpenAI(**self._client_kwargs())
        return self._client

    def _get_async_client(self) -> Any:
        if self._async_client is not None:
            return self._async_client
        from openai import AsyncOpenAI

        self._async_client = AsyncOpenAI(**self._client_kwargs())
        return self._async_client

    async def close(self) -> None:
        clients = [self._async_client, self._client, *self._retired_clients]
        self._async_client = None
        self._client = None
        self._retired_clients.clear()
        for client in clients:
            if client is None:
                continue
            close = getattr(client, "close", None)
            if close is None:
                continue
            result = close()
            if asyncio.iscoroutine(result):
                await result

    async def is_healthy(self) -> tuple[bool, str]:
        try:
            self._get_async_client()
            return True, ""
        except Exception as exc:
            return False, str(exc)

    async def list_models(self) -> tuple[list[str], str]:
        try:
            response = await self._get_async_client().models.list()
            ids = [str(getattr(model, "id", "") or "") for model in getattr(response, "data", []) or []]
            return sorted({model_id for model_id in ids if model_id}), ""
        except Exception as exc:
            return [], str(exc)

    async def generate(self, prompt: str, model: str, temperature: float = 0.7) -> LLMResponse:
        try:
            response = await self._get_async_client().chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=float(temperature),
            )
            try:
                text = response.choices[0].message.content or ""
            except Exception:
                text = str(response)
            return LLMResponse(text=text, raw=response)
        except Exception as exc:
            return LLMResponse(text=f"[OpenAI error] {exc}", raw=None)

    async def stream_generate(self, prompt: str, model: str, temperature: float = 0.7) -> AsyncIterator[str]:
        try:
            stream = await self._get_async_client().chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=float(temperature),
                stream=True,
            )
            async for event in stream:
                try:
                    content = event.choices[0].delta.content
                except Exception:
                    content = None
                if content:
                    yield str(content)
        except Exception as exc:
            yield f"[OpenAI error] {exc}"

    async def transcribe_audio(
        self,
        audio_path: Path,
        model: str = "gpt-4o-transcribe",
        prompt: str = "",
    ) -> tuple[str, str]:
        try:
            audio_bytes = await asyncio.to_thread(audio_path.read_bytes)
            kwargs: dict[str, Any] = {
                "model": model,
                "file": (audio_path.name, audio_bytes, "audio/wav"),
                "temperature": 0.0,
                "language": "en",
            }
            if prompt.strip():
                kwargs["prompt"] = prompt.strip()
            if self._async_client is None and self._client is not None:
                create = self._client.audio.transcriptions.create
                response = await asyncio.to_thread(create, **kwargs)
            else:
                response = await self._get_async_client().audio.transcriptions.create(**kwargs)
            text = str(getattr(response, "text", "") or "").strip()
            return text, ""
        except Exception as exc:
            return "", str(exc)
