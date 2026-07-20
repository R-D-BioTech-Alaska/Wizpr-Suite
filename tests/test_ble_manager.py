from __future__ import annotations

import unittest
from unittest.mock import patch

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from wizpr_suite.ble.ble_manager import BLEManager, WIZPR_RING_SERVICE_UUID


def _adv(name: str, address: str, rssi: int = -45) -> tuple[BLEDevice, AdvertisementData]:
    return (
        BLEDevice(address, name, None),
        AdvertisementData(
            local_name=name,
            manufacturer_data={},
            service_data={},
            service_uuids=[WIZPR_RING_SERVICE_UUID],
            tx_power=None,
            rssi=rssi,
            platform_data=(),
        ),
    )


class BleManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_wizpr_scan_returns_first_ring(self) -> None:
        class FakeScanner:
            def __init__(self, detection_callback=None, **_kwargs) -> None:
                self.detection_callback = detection_callback

            async def start(self) -> None:
                device, adv = _adv("WIZPR RING-AA:01", "AA:BB:CC:DD:EE:01")
                self.detection_callback(device, adv)

            async def stop(self) -> None:
                pass

        with patch("wizpr_suite.ble.ble_manager.BleakScanner", FakeScanner):
            devices, match = await BLEManager().scan_wizpr_live(seconds=5.0)

        self.assertIsNotNone(match)
        self.assertEqual("AA:BB:CC:DD:EE:01", match.address)
        self.assertEqual(["AA:BB:CC:DD:EE:01"], [dev.address for dev in devices])

    async def test_live_wizpr_scan_prefers_saved_ring(self) -> None:
        class FakeScanner:
            def __init__(self, detection_callback=None, **_kwargs) -> None:
                self.detection_callback = detection_callback

            async def start(self) -> None:
                first, first_adv = _adv("WIZPR RING-AA:01", "AA:BB:CC:DD:EE:01", rssi=-35)
                saved, saved_adv = _adv("WIZPR RING-AA:02", "AA:BB:CC:DD:EE:02", rssi=-80)
                self.detection_callback(first, first_adv)
                self.detection_callback(saved, saved_adv)

            async def stop(self) -> None:
                pass

        with patch("wizpr_suite.ble.ble_manager.BleakScanner", FakeScanner):
            _devices, match = await BLEManager().scan_wizpr_live(
                seconds=5.0,
                preferred_address="AA:BB:CC:DD:EE:02",
            )

        self.assertIsNotNone(match)
        self.assertEqual("AA:BB:CC:DD:EE:02", match.address)


if __name__ == "__main__":
    unittest.main()
