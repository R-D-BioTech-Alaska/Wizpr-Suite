from __future__ import annotations

import argparse
import asyncio
from datetime import datetime

from ..ble.ble_manager import BLEManager, DiscoveredDevice

def _format_device(dev: DiscoveredDevice) -> str:
    parts = [
        f"{dev.candidate_label:24}",
        f"RSSI={dev.rssi:4}",
        f"name={dev.name or '(no name)'}",
        f"addr={dev.address}",
    ]
    if dev.service_uuids:
        parts.append(f"services={','.join(dev.service_uuids)}")
    if dev.manufacturer_data:
        parts.append(f"mfg={dev.manufacturer_data}")
    if dev.service_data:
        parts.append(f"service_data={dev.service_data}")
    return " ".join(parts)

async def _run_scan(seconds: float, chunk_seconds: float) -> int:
    manager = BLEManager()
    seen: dict[str, DiscoveredDevice] = {}
    found_ring = False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds

    print("WIZPR guided BLE scan")
    print("1. Turn off Bluetooth on the phone, or fully close the official WIZPR app.")
    print("2. Keep the ring out of the case and within a few feet of the PC.")
    print("3. Press the ring button while this scan is running.")
    print()

    while loop.time() < deadline:
        remaining = max(0.0, deadline - loop.time())
        scan_for = min(chunk_seconds, remaining)
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{stamp}] SDK-style scan for {scan_for:.0f}s, {remaining:.0f}s left...")
        try:
            devices = await manager.scan_wizpr(seconds=scan_for, include_reverse_ble=True)
        except Exception as exc:
            print()
            print(f"Scanner error: {exc}")
            print("Windows could not start BLE scanning through the active Bluetooth radio.")
            print("Classic Bluetooth devices can still pair while BLE/GATT scanning is unavailable.")
            print("If you are using a UD100, keep the Intel adapter disabled if that is intentional,")
            print("then unplug/replug the UD100 and confirm Device Manager shows a present Microsoft Bluetooth LE Enumerator.")
            return 3

        for dev in devices:
            previous = seen.get(dev.address)
            if previous is None or dev.rssi > previous.rssi:
                seen[dev.address] = dev
            if previous is None:
                print("  new:", _format_device(dev))

        rings = [d for d in seen.values() if d.candidate_label == "WIZPR ring"]
        if rings:
            rings.sort(key=lambda d: d.rssi, reverse=True)
            print()
            print("FOUND WIZPR RING CANDIDATE:")
            print(" ", _format_device(rings[0]))
            found_ring = True
            break

    print()
    print("Summary:")
    for dev in sorted(seen.values(), key=BLEManager._device_sort_key):
        print(" ", _format_device(dev))

    if found_ring:
        return 0

    print()
    print("No WIZPR ring advertisement was seen during SDK-style discovery.")
    print("Run ble_watch if you want a raw all-device capture for whatever changes exactly when the ring is used.")
    return 2

def main() -> None:
    parser = argparse.ArgumentParser(description="Guided WIZPR Ring BLE discovery scan.")
    parser.add_argument("--seconds", type=float, default=90.0, help="Total scan time.")
    parser.add_argument("--chunk", type=float, default=5.0, help="Seconds per scan chunk.")
    args = parser.parse_args()

    try:
        code = asyncio.run(_run_scan(max(args.seconds, 1.0), max(args.chunk, 1.0)))
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)

if __name__ == "__main__":
    main()
