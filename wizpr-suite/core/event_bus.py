from __future__ import annotations
from typing import Awaitable, Callable, Any, DefaultDict
from collections import defaultdict
from .logging_setup import get_logger

Handler = Callable[[Any], Awaitable[None]]
logger = get_logger("wizpr_suite.events")

class EventBus:
    def __init__(self) -> None:
        self._subs: DefaultDict[str, list[Handler]] = defaultdict(list)

    async def subscribe(self, topic: str, handler: Handler) -> None:
        self._subs[topic].append(handler)

    async def publish(self, topic: str, payload: Any = None) -> None:
        if topic not in self._subs:
            return
        for h in list(self._subs[topic]):
            try:
                await h(payload)
            except Exception:
                logger.exception("Event handler failed for topic %s", topic)
                continue
