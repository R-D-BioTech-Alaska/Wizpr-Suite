from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from ..ble.ble_manager import BLEManager, DiscoveredDevice, WIZPR_REVERSE_BLE_SCAN_SERVICE_UUIDS
from ..core.config import get_default_app_dir


def _adv_to_device(device: BLEDevice, adv: AdvertisementData) -> DiscoveredDevice:
    address = BLEManager._normalize_address(device.address)
    name = (adv.local_name or device.name or "").strip()
    service_uuids = [str(u) for u in (getattr(adv, "service_uuids", []) or [])]
    manufacturer_data = {
        f"0x{company_id:04x}": bytes(data).hex()
        for company_id, data in (getattr(adv, "manufacturer_data", {}) or {}).items()
    }
    service_data = {
        str(uuid): bytes(data).hex()
        for uuid, data in (getattr(adv, "service_data", {}) or {}).items()
    }
    tx_power = getattr(adv, "tx_power", None)
    return DiscoveredDevice(
        address=address,
        name=name,
        rssi=int(getattr(adv, "rssi", 0) or 0),
        service_uuids=service_uuids,
        tx_power=int(tx_power) if tx_power is not None else None,
        manufacturer_data=manufacturer_data,
        service_data=service_data,
    )


def _device_record(dev: DiscoveredDevice) -> dict[str, Any]:
    return {
        "address": dev.address,
        "name": dev.name,
        "rssi": dev.rssi,
        "label": dev.candidate_label,
        "service_uuids": dev.service_uuids or [],
        "manufacturer_data": dev.manufacturer_data or {},
        "service_data": dev.service_data or {},
        "tx_power": dev.tx_power,
    }


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


def _capture_path() -> Path:
    out_dir = get_default_app_dir() / "captures"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return out_dir / f"ble-watch-{stamp}.jsonl"


async def _run(seconds: float, print_all: bool, min_rssi: int, output: Path | None, official_filters: bool) -> int:
    manager = BLEManager()
    repair = await manager.ensure_bluetooth_radio_on()
    if repair.get("radio_on") is False:
        print(BLEManager.format_radio_repair_result(repair))
        return 4

    path = output or _capture_path()
    seen: dict[str, dict[str, Any]] = {}
    found_ring = asyncio.Event()
    started = datetime.now()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(seconds, 1.0)

    print("WIZPR BLE advertisement watch")
    print("Keep this running while using the ring; this captures the advertisements Windows reports.")
    if official_filters:
        print("mode: official WIZPR service filters (ring/case/BLE-client)")
    else:
        print("mode: raw all-device advertisements")
    print(f"capture: {path}")
    print()

    fh = path.open("a", encoding="utf-8")

    def callback(device: BLEDevice, adv: AdvertisementData) -> None:
        dev = _adv_to_device(device, adv)
        if dev.rssi and dev.rssi < min_rssi:
            return

        record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
            **_device_record(dev),
        }
        fh.write(json.dumps(record, sort_keys=True) + "\n")
        fh.flush()

        previous = seen.get(dev.address)
        signature = {
            "name": dev.name,
            "label": dev.candidate_label,
            "service_uuids": dev.service_uuids or [],
            "manufacturer_data": dev.manufacturer_data or {},
            "service_data": dev.service_data or {},
        }
        seen[dev.address] = signature

        is_interesting = (
            print_all
            or previous is None
            or previous != signature
            or dev.candidate_label == "WIZPR ring"
        )
        if is_interesting:
            stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"[{stamp}] {_format_device(dev)}", flush=True)

        if dev.candidate_label == "WIZPR ring":
            found_ring.set()

    scanner = BleakScanner(
        detection_callback=callback,
        service_uuids=WIZPR_REVERSE_BLE_SCAN_SERVICE_UUIDS if official_filters else None,
        scanning_mode="active",
    )
    try:
        await scanner.start()
    except Exception as exc:
        fh.close()
        raise BLEManager._scanner_start_error(exc, repair) from exc

    try:
        while loop.time() < deadline and not found_ring.is_set():
            await asyncio.sleep(0.25)
    finally:
        await scanner.stop()
        fh.close()

    print()
    print("Summary:")
    known = []
    if path.exists():
        with path.open("r", encoding="utf-8") as fh2:
            latest: dict[str, dict[str, Any]] = {}
            for line in fh2:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                latest[item["address"]] = item
            known = sorted(latest.values(), key=lambda d: (d.get("label") != "WIZPR ring", -(d.get("rssi") or -999)))

    for item in known:
        dev = DiscoveredDevice(
            address=item.get("address", ""),
            name=item.get("name", ""),
            rssi=int(item.get("rssi") or 0),
            service_uuids=item.get("service_uuids") or [],
            manufacturer_data=item.get("manufacturer_data") or {},
            service_data=item.get("service_data") or {},
            tx_power=item.get("tx_power"),
        )
        print(" ", _format_device(dev))

    return 0 if any(item.get("label") == "WIZPR ring" for item in known) else 2


def main() -> None:
    parser = argparse.ArgumentParser(description="Continuously log BLE advertisements for WIZPR Ring discovery.")
    parser.add_argument("--seconds", type=float, default=120.0, help="How long to watch.")
    parser.add_argument("--all", action="store_true", help="Print every advertisement update, not just new/changed devices.")
    parser.add_argument("--min-rssi", type=int, default=-100, help="Ignore weaker advertisements.")
    parser.add_argument("--output", type=Path, default=None, help="Optional JSONL capture path.")
    parser.add_argument("--official-filters", action="store_true", help="Use the official app's WIZPR ring/case/BLE-client service filters.")
    args = parser.parse_args()

    try:
        code = asyncio.run(_run(args.seconds, args.all, args.min_rssi, args.output, args.official_filters))
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)


if __name__ == "__main__":
    main()
