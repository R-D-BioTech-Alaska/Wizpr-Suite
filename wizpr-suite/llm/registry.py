from __future__ import annotations

from typing import Dict, List

from .base import LLMProvider


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: Dict[str, LLMProvider] = {}

    def register(self, provider: LLMProvider) -> None:
        self._providers[provider.id] = provider

    def get(self, pid: str) -> LLMProvider | None:
        return self._providers.get(pid)

    def list_ids(self) -> List[str]:
        return sorted(self._providers.keys())

    def list_providers(self) -> List[LLMProvider]:
        return [self._providers[k] for k in self.list_ids()]
