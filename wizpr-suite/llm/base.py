from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class LLMResponse:
    text: str
    raw: Any = None


class LLMProvider(Protocol):
    id: str
    display_name: str

    async def is_healthy(self) -> tuple[bool, str]:
        ...

    async def list_models(self) -> tuple[list[str], str]:
        ...

    async def generate(self, prompt: str, model: str, temperature: float = 0.7) -> LLMResponse:
        ...

    def stream_generate(self, prompt: str, model: str, temperature: float = 0.7) -> AsyncIterator[str]:
        ...

    async def close(self) -> None:
        ...
