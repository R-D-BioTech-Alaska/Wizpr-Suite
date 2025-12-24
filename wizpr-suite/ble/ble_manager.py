from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from bleak import BleakClient, BleakScanner 
from bleak.backends.device import BLEDevice  
from bleak.backends.scanner import AdvertisementData  

from ..core.logging_setup import get_logger

logger = get_logger("wizpr_suite.ble")


class DiscoveredDevice:
    address: str
    name: str
    rssi: int


class BLEManager:
    def __init__(self) -> None:
        self._client: BleakClient | None = None

    async def scan(self, seconds: float = 5.0) -> list[DiscoveredDevice]:
        found: Dict[str, Tuple[BLEDevice, AdvertisementData]] = {}

        def _cb(device: BLEDevice, adv: AdvertisementData) -> None:
            found[device.address] = (device, adv)

        scanner = BleakScanner(detection_callback=_cb)
        await scanner.start()
        try:
            await asyncio.sleep(seconds)
        finally:
            await scanner.stop()

        out: list[DiscoveredDevice] = []
        for addr, (dev, adv) in found.items():
            name = (adv.local_name or dev.name or "").strip()
            rssi = int(getattr(adv, "rssi", 0) or 0)
            out.append(DiscoveredDevice(address=addr, name=name, rssi=rssi))
        out.sort(key=lambda d: d.rssi, reverse=True)
        return out

    async def connect(self, address: str, timeout: float = 12.0) -> BleakClient:
        await self.disconnect()

        device = None
        try:
            device = await BleakScanner.find_device_by_address(address, timeout=timeout)
        except Exception:
            device = None

        client = BleakClient(device or address, timeout=timeout)

        try:
            await client.connect()
        except Exception as first_err:
            try:
                await asyncio.sleep(0.5)
                device2 = await BleakScanner.find_device_by_address(address, timeout=timeout)
                client = BleakClient(device2 or address, timeout=timeout)
                await client.connect()
            except Exception:
                raise first_err

        if not client.is_connected:
            raise RuntimeError(f"Failed to connect to {address}")

        self._client = client
        logger.info("Connected BLE: %s", address)
        return client

    async def disconnect(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        finally:
            self._client = None
            logger.info("Disconnected BLE.")

    def client(self) -> BleakClient | None:
        return self._client
