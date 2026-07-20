from __future__ import annotations

import argparse
import asyncio

from ..ble.ble_manager import BLEManager, DiscoveredDevice
from ..ble.ring_controller import RingController, RingProfile, WIZPR_DEVICE_NAME_PREFIX
from ..core.event_bus import EventBus


def _show_device(prefix: str, dev: DiscoveredDevice) -> None:
    services = ", ".join(dev.service_uuids or []) or "-"
    print(f"{prefix}: {dev.candidate_label} {dev.name or '(no name)'} [{dev.address}] services={services}")


async def _select_device(ble: BLEManager, address: str, scan_seconds: float) -> DiscoveredDevice | None:
    if address:
        clean = BLEManager._normalize_address(address)
        return DiscoveredDevice(address=clean, name="Manual BLE address", rssi=0, service_uuids=[])

    loop = asyncio.get_running_loop()
    started = loop.time()
    print(f"Scanning for {WIZPR_DEVICE_NAME_PREFIX} for {scan_seconds:.0f}s with SDK-style discovery...")
    try:
        devs = await ble.scan_wizpr(seconds=scan_seconds, include_reverse_ble=True)
    except Exception as exc:
        print(f"Live BLE scanner failed immediately: {exc}")
        devs = []
    else:
        for item in devs:
            _show_device("scan", item)
        ring = next((d for d in devs if d.candidate_label == "WIZPR ring"), None)
        if ring is not None:
            return ring

    remaining = max(0.0, scan_seconds - (loop.time() - started))
    print(f"No WIZPR advertisement found in live scan. Watching Windows Bluetooth LE device lists for {remaining:.0f}s...")
    seen: dict[str, DiscoveredDevice] = {}
    deadline = loop.time() + remaining
    while True:
        for item in await ble.windows_associated_devices():
            old = seen.get(item.address)
            seen[item.address] = item
            if old is None:
                _show_device("windows", item)
        ring = next((d for d in seen.values() if d.candidate_label == "WIZPR ring"), None)
        if ring is not None:
            return ring
        if loop.time() >= deadline:
            break
        await asyncio.sleep(min(1.0, deadline - loop.time()))

    return next((d for d in seen.values() if d.candidate_label == "WIZPR ring"), None)


async def _show_ble_health_gate(ble: BLEManager, ignore_adapter_gate: bool) -> bool:
    report = await ble.health_report()
    print(BLEManager.format_health_report(report))
    if ignore_adapter_gate:
        return True
    return report.get("adapter_has_ble_central") is not False


async def main_async(address: str, scan_seconds: float, listen_seconds: float, ignore_adapter_gate: bool) -> int:
    ble = BLEManager()
    bus = EventBus()
    ring = RingController(ble, bus, RingProfile())

    if not await _show_ble_health_gate(ble, ignore_adapter_gate):
        if not address:
            print("Stopping before scan: Windows reports no BLE Central capability on the active Bluetooth radio.")
            return 4
        print("Continuing with manual address because --address was supplied, but BLE GATT is likely to fail.")

    async def printer(topic: str):
        async def _handler(payload):
            print(f"{topic}: {payload}")

        return _handler

    for topic in ("raw_notify", "ring_event", "battery", "version", "mic_pre_on", "mic_on", "mic_off", "audio_capture"):
        await bus.subscribe(topic, await printer(topic))

    dev = await _select_device(ble, address, scan_seconds)
    if dev is None:
        cases = [d for d in await ble.windows_associated_devices() if d.candidate_label == "WIZPR case"]
        if cases:
            print("WIZPR case is visible, but the ring itself is not visible to Windows right now:")
            for case in cases:
                _show_device("case", case)
            print("The ring should advertise separately as WIZPR RING-xx:xx. The case address is not the ring address.")
            print("To inspect the case only, run: py -m wizpr_suite.tools.case_probe")
        clients = [d for d in await ble.windows_associated_devices() if d.candidate_label == "WIZPR BLE client"]
        if clients:
            print("WIZPR BLE client device(s) are visible, but those are companion/client channels, not the ring:")
            for client in clients:
                _show_device("ble_client", client)
        print("No WIZPR ring candidate found. If Windows can see the ring address, pass --address AA:BB:CC:DD:EE:FF.")
        return 2

    _show_device("selected", dev)
    ring.profile.address = dev.address

    try:
        await ring.connect(pair=False)
        print("connected")
        try:
            await ring.start_wizpr_session()
        except Exception as exc:
            print(f"basic setup failed: {exc}")
            return 3

        summary = await ring.gatt_summary()
        for service in summary:
            print(f"service {service['uuid']}")
            for char in service.get("characteristics", []):
                print(f"  char {char['uuid']} props={','.join(char.get('properties', []))}")

        print(f"Listening for {listen_seconds:.0f}s. Press the ring button or raise it to speak.")
        await asyncio.sleep(listen_seconds)
        return 0
    finally:
        try:
            await ring.disconnect()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Connect to a WIZPR Ring and print BLE events.")
    parser.add_argument("--address", default="", help="Known BLE address, e.g. 28:76:81:FA:97:22.")
    parser.add_argument("--scan-seconds", type=float, default=12.0)
    parser.add_argument("--listen-seconds", type=float, default=30.0)
    parser.add_argument("--ignore-adapter-gate", action="store_true", help="Try anyway even if Windows says BLE Central is unavailable.")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main_async(args.address, args.scan_seconds, args.listen_seconds, args.ignore_adapter_gate)))


if __name__ == "__main__":
    main()
