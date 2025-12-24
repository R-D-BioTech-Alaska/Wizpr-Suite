from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Any


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
