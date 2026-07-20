from __future__ import annotations

import argparse
import asyncio

from ..ble.ble_manager import BLEManager, DiscoveredDevice


def _show_device(prefix: str, dev: DiscoveredDevice) -> None:
    services = ", ".join(dev.service_uuids or []) or "-"
    print(f"{prefix}: {dev.candidate_label} {dev.name or '(no name)'} [{dev.address}] services={services}")


async def main_async(show_all: bool, turn_on_radio: bool) -> int:
    ble = BLEManager()
    if turn_on_radio:
        result = await ble.ensure_bluetooth_radio_on()
        print(BLEManager.format_radio_repair_result(result))

    report = await ble.health_report()
    print(BLEManager.format_health_report(report))

    if show_all:
        print("windows_associated_devices:")
        for dev in report["associated_devices"]:
            _show_device("  associated", dev)
        print("windows_cached_devices:")
        for dev in report["cached_devices"]:
            _show_device("  cached", dev)

    if report.get("adapter_has_ble_central") is False:
        return 4
    if not report.get("scanner_ok"):
        return 3
    if not report.get("ring_candidates"):
        return 2
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Check whether Windows can expose BLE Central/GATT for WIZPR Ring.")
    parser.add_argument("--all", action="store_true", help="Print every Windows-associated/cached BLE device.")
    parser.add_argument("--turn-on-radio", action="store_true", help="Ask Windows to turn on the Bluetooth radio before testing.")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main_async(show_all=args.all, turn_on_radio=args.turn_on_radio)))


if __name__ == "__main__":
    main()
