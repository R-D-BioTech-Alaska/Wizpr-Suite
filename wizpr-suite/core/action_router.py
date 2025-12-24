from __future__ import annotations

from typing import Any, Callable, Awaitable, Dict


ActionHandler = Callable[[dict[str, Any]], Awaitable[None]]


class ActionRouter:
    def __init__(self) -> None:
        self._handlers: Dict[str, ActionHandler] = {}

    def register_action_handler(self, action: str, handler: ActionHandler) -> None:
        self._handlers[action] = handler

    async def dispatch(self, action: str, payload: dict[str, Any] | None = None) -> None:
        h = self._handlers.get(action)
        if not h:
            return
        await h(payload or {"action": action})
