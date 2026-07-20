from __future__ import annotations

import argparse
import asyncio
import string

from ..ble.ble_manager import BLEManager, DiscoveredDevice
from ..ble.ring_controller import WIZPR_SERVICE_UUID

WIZPR_CASE_SERVICE_UUID = "00000000-dc2e-4362-93d3-df429eb3ad11"
WIZPR_CASE_EVENT_CHAR_UUID = "00000008-dc2e-4362-93d3-df429eb3ad11"


def _show_device(prefix: str, dev: DiscoveredDevice) -> None:
    services = ", ".join(dev.service_uuids or []) or "-"
    print(f"{prefix}: {dev.candidate_label} {dev.name or '(no name)'} [{dev.address}] services={services}")


def _value_preview(data: bytes, limit: int = 80) -> str:
    hex_value = data[:limit].hex()
    if len(data) > limit:
        hex_value += "..."
    text = "".join(chr(b) if chr(b) in string.printable and b not in (10, 13, 9) else "." for b in data[:limit])
    if len(data) > limit:
        text += "..."
    return f"hex={hex_value or '-'} ascii={text or '-'}"


def _decode_text(data: bytes) -> str:
    return data.rstrip(b"\x00").decode("utf-8", errors="replace").strip()


async def _select_case(ble: BLEManager, address: str, scan_seconds: float) -> DiscoveredDevice | None:
    if address:
        clean = BLEManager._normalize_address(address)
        return DiscoveredDevice(address=clean, name="Manual WIZPR case address", rssi=0, service_uuids=[])

    loop = asyncio.get_running_loop()
    started = loop.time()
    print(f"Scanning for WIZPR CASE for {scan_seconds:.0f}s with the official WIZPR service filters...")
    try:
        devs = await ble.scan_wizpr(seconds=scan_seconds, include_reverse_ble=True)
    except Exception as exc:
        print(f"Live BLE scanner failed immediately: {exc}")
        devs = []
    else:
        for item in devs:
            _show_device("scan", item)
        case = next((d for d in devs if d.candidate_label == "WIZPR case"), None)
        if case is not None:
            return case

    remaining = max(0.0, scan_seconds - (loop.time() - started))
    print(f"No WIZPR case found in live scan. Watching Windows Bluetooth LE device lists for {remaining:.0f}s...")
    seen: dict[str, DiscoveredDevice] = {}
    deadline = loop.time() + remaining
    while True:
        for item in await ble.windows_associated_devices():
            old = seen.get(item.address)
            seen[item.address] = item
            if old is None:
                _show_device("windows", item)
        case = next((d for d in seen.values() if d.candidate_label == "WIZPR case"), None)
        if case is not None:
            return case
        if loop.time() >= deadline:
            break
        await asyncio.sleep(min(1.0, deadline - loop.time()))

    return next((d for d in seen.values() if d.candidate_label == "WIZPR case"), None)


async def main_async(
    address: str,
    scan_seconds: float,
    connect_timeout: float,
    read_values: bool,
    read_timeout: float,
    listen_seconds: float,
    commands: list[str],
) -> int:
    ble = BLEManager()
    report = await ble.health_report()
    print(BLEManager.format_health_report(report))
    if report.get("adapter_has_ble_central") is False and not address:
        print("Stopping before case probe: Windows reports no BLE Central capability on the active Bluetooth radio.")
        return 4

    case = await _select_case(ble, address, scan_seconds)
    if case is None:
        print("No WIZPR case candidate found.")
        return 2

    _show_device("selected", case)
    try:
        try:
            client = await ble.connect(case.address, timeout=connect_timeout, pair=False)
        except Exception as exc:
            print(f"connect_failed: {exc}")
            print(
                "Windows can list this WIZPR case entry, but it cannot open a BLE GATT connection to it right now. "
                "That cached case entry is not enough to discover or control the ring."
            )
            return 3

        print("connected")

        service_uuids = {str(s.uuid).lower() for s in client.services}
        if WIZPR_SERVICE_UUID in service_uuids:
            print("This device exposes the WIZPR ring service. It may be the ring, not the case.")
        elif WIZPR_CASE_SERVICE_UUID in service_uuids:
            print("This device exposes the WIZPR case service.")
        else:
            print("This device does not expose the known WIZPR ring or case service UUID.")

        readable = []
        case_event_found = False
        for service in client.services:
            print(f"service {service.uuid} {getattr(service, 'description', '')}")
            for char in service.characteristics:
                props = [str(p).lower() for p in (getattr(char, "properties", []) or [])]
                print(f"  char {char.uuid} props={','.join(props) or '-'} {getattr(char, 'description', '')}")
                if str(char.uuid).lower() == WIZPR_CASE_EVENT_CHAR_UUID:
                    case_event_found = True
                if "read" in props:
                    readable.append(char.uuid)

        if read_values and readable:
            print("reading advertised readable characteristics...")
            for char_uuid in readable:
                try:
                    data = await asyncio.wait_for(client.read_gatt_char(char_uuid), timeout=read_timeout)
                except Exception as exc:
                    print(f"  read {char_uuid}: failed: {exc}")
                else:
                    print(f"  read {char_uuid}: {_value_preview(bytes(data))}")

        if case_event_found and listen_seconds > 0:
            received: list[str] = []

            def _on_case_event(_sender: int, data: bytearray) -> None:
                payload = bytes(data)
                text = _decode_text(payload)
                received.append(text)
                print(f"notify {WIZPR_CASE_EVENT_CHAR_UUID}: {_value_preview(payload)} text={text or '-'}")

            print(f"subscribing to case event characteristic for {listen_seconds:.0f}s...")
            await client.start_notify(WIZPR_CASE_EVENT_CHAR_UUID, _on_case_event)
            try:
                for command in commands:
                    command = command.strip()
                    if not command:
                        continue
                    print(f"write {WIZPR_CASE_EVENT_CHAR_UUID}: {command}")
                    await client.write_gatt_char(WIZPR_CASE_EVENT_CHAR_UUID, command.encode("utf-8"), response=False)
                    await asyncio.sleep(0.5)
                await asyncio.sleep(listen_seconds)
            finally:
                try:
                    await client.stop_notify(WIZPR_CASE_EVENT_CHAR_UUID)
                except Exception:
                    pass

            if not received:
                print("No case notifications received during the listen window.")
        elif not case_event_found:
            print(f"Case event characteristic not found: {WIZPR_CASE_EVENT_CHAR_UUID}")

        return 0
    finally:
        try:
            await ble.disconnect()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Passively inspect a WIZPR charging case BLE peripheral.")
    parser.add_argument("--address", default="", help="Known BLE address, e.g. 18:7A:3E:F3:FF:4E.")
    parser.add_argument("--scan-seconds", type=float, default=12.0)
    parser.add_argument("--connect-timeout", type=float, default=18.0)
    parser.add_argument("--read-timeout", type=float, default=3.0)
    parser.add_argument("--no-read", action="store_true", help="Do not read characteristics, only list GATT metadata.")
    parser.add_argument("--listen-seconds", type=float, default=8.0, help="How long to listen for case notifications after writes.")
    parser.add_argument(
        "--command",
        action="append",
        default=None,
        help="Case command to write after subscribing. Repeat for multiple commands.",
    )
    args = parser.parse_args()
    commands = args.command or ["CRADLE_BAT"]
    raise SystemExit(
        asyncio.run(
            main_async(
                address=args.address,
                scan_seconds=args.scan_seconds,
                connect_timeout=args.connect_timeout,
                read_values=not args.no_read,
                read_timeout=args.read_timeout,
                listen_seconds=args.listen_seconds,
                commands=commands,
            )
        )
    )


if __name__ == "__main__":
    main()
