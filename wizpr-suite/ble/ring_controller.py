from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from bleak import BleakClient  # type: ignore

from ..core.event_bus import EventBus
from ..core.logging_setup import get_logger
from .ble_manager import BLEManager

logger = get_logger("wizpr_suite.ring")


class RingProfile:
    address: str = ""


# Again, currently no ring or sdk. Will update as it becomes available.
class RingController:
    """
    Generic BLE ring controller.

    Since Wizpr’s exact GATT is not public, we will use the following:
    - connect/disconnect
    - GATT inspection
    - subscribe helper for notify characteristics
    - event publishing to bus topics (raw_notify, button_single/double/long if detected)
    """

    def __init__(self, ble: BLEManager, bus: EventBus, profile: RingProfile) -> None:
        self.ble = ble
        self.bus = bus
        self.profile = profile
        self._notify_handlers: Dict[str, Callable[[bytearray], None]] = {}

    async def connect(self) -> None:
        if not self.profile.address:
            raise RuntimeError("No BLE address set.")
        await self.ble.connect(self.profile.address)

    async def disconnect(self) -> None:
        await self.ble.disconnect()

    async def gatt_summary(self) -> list[dict[str, Any]]:
        client = self.ble.client
        if client is None:
            return []
        services = client.services
        out: list[dict[str, Any]] = []
        for s in services:
            sdict: dict[str, Any] = {"uuid": str(s.uuid), "description": str(getattr(s, "description", "")), "characteristics": []}
            for c in s.characteristics:
                sdict["characteristics"].append({
                    "uuid": str(c.uuid),
                    "properties": list(getattr(c, "properties", []) or []),
                    "description": str(getattr(c, "description", "")),
                })
            out.append(sdict)
        return out

    async def subscribe(self, char_uuid: str) -> None:
        client = self.ble.client
        if client is None:
            raise RuntimeError("Not connected")

        async def _publish(payload: dict[str, Any]) -> None:
            await self.bus.publish("raw_notify", payload)

        def _cb(_sender: int, data: bytearray) -> None:
            # publish raw
            asyncio.create_task(_publish({"uuid": char_uuid, "data_hex": data.hex()}))

            # button events: if device emits ASCII tokens, map them
            try:
                txt = bytes(data).decode("utf-8", errors="ignore").strip().lower()
            except Exception:
                txt = ""

            if txt in ("single", "button_single", "tap"):
                asyncio.create_task(self.bus.publish("button_single", {"uuid": char_uuid, "text": txt}))
            elif txt in ("double", "button_double", "dbl"):
                asyncio.create_task(self.bus.publish("button_double", {"uuid": char_uuid, "text": txt}))
            elif txt in ("long", "button_long", "hold"):
                asyncio.create_task(self.bus.publish("button_long", {"uuid": char_uuid, "text": txt}))

        await client.start_notify(char_uuid, _cb)
        self._notify_handlers[char_uuid] = _cb
        logger.info("Subscribed notify: %s", char_uuid)

    async def unsubscribe(self, char_uuid: str) -> None:
        client = self.ble.client
        if client is None:
            return
        try:
            await client.stop_notify(char_uuid)
        except Exception:
            pass
        self._notify_handlers.pop(char_uuid, None)
        logger.info("Unsubscribed notify: %s", char_uuid)
