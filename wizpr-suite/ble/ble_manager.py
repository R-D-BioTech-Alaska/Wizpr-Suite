from __future__ import annotations

import asyncio
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Dict, Tuple

try:
    import winreg
except ImportError:
    winreg = None

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

try:
    from bleak.backends.winrt.scanner import RawAdvData
except ImportError:
    RawAdvData = None

from ..core.logging_setup import get_logger

logger = get_logger("wizpr_suite.ble")

WIZPR_RING_SERVICE_UUID = "00000000-dc2e-4362-93d3-df429eb3ad10"
WIZPR_CASE_SERVICE_UUID = "00000000-dc2e-4362-93d3-df429eb3ad11"
WIZPR_BLE_CLIENT_SERVICE_UUID = "00000000-dc2e-4362-93d3-df429eb3ad12"
WIZPR_BLE_CLIENT_CHAR_UUID = "00000001-dc2e-4362-93d3-df429eb3ad12"
WIZPR_REVERSE_BLE_MANUFACTURER_KEY = "0x0419"
WIZPR_OFFICIAL_SCAN_SERVICE_UUIDS = [WIZPR_RING_SERVICE_UUID, WIZPR_CASE_SERVICE_UUID]
WIZPR_REVERSE_BLE_SCAN_SERVICE_UUIDS = [
    WIZPR_RING_SERVICE_UUID,
    WIZPR_CASE_SERVICE_UUID,
    WIZPR_BLE_CLIENT_SERVICE_UUID,
]
WIZPR_RING_LABEL = "WIZPR ring"
WIZPR_RELATED_LABELS = {WIZPR_RING_LABEL, "WIZPR case", "WIZPR BLE client"}
WIZPR_SILABS_OUI = "28:76:81"
WIZPR_IOTBT_SERVICE_UUID = "00005a00-0000-1000-8000-00805f9b34fb"


@dataclass
class DiscoveredDevice:
    address: str
    name: str
    rssi: int
    service_uuids: list[str] | None = None
    tx_power: int | None = None
    manufacturer_data: dict[str, str] | None = None
    service_data: dict[str, str] | None = None

    @property
    def candidate_label(self) -> str:
        raw_name = (self.name or "").strip().casefold()
        name_norm = BLEManager._norm(self.name)
        services = {u.casefold() for u in (self.service_uuids or [])}
        service_data_keys = {u.casefold() for u in (self.service_data or {}).keys()}
        manufacturer_keys = {k.casefold() for k in (self.manufacturer_data or {}).keys()}

        if WIZPR_CASE_SERVICE_UUID in services or WIZPR_CASE_SERVICE_UUID in service_data_keys or "wizpr case" in name_norm:
            return "WIZPR case"
        if (
            WIZPR_BLE_CLIENT_SERVICE_UUID in services
            or WIZPR_BLE_CLIENT_SERVICE_UUID in service_data_keys
            or raw_name.startswith("wizpr|")
            or WIZPR_REVERSE_BLE_MANUFACTURER_KEY in manufacturer_keys
        ):
            return "WIZPR BLE client"
        if (
            WIZPR_RING_SERVICE_UUID in services
            or WIZPR_RING_SERVICE_UUID in service_data_keys
            or self._name_looks_like_wizpr_ring(name_norm)
        ):
            return "WIZPR ring"
        if self.address.upper().startswith(WIZPR_SILABS_OUI):
            return "Possible WIZPR/Silicon Labs"
        if (
            name_norm.startswith("iotbt")
            or WIZPR_IOTBT_SERVICE_UUID in services
            or WIZPR_IOTBT_SERVICE_UUID in service_data_keys
            or "0x5a00" in manufacturer_keys
        ):
            return "Unknown IoT/LED-like"
        if "0x004c" in manufacturer_keys:
            return "Nearby Apple BLE"
        if not self.name and not self.service_uuids:
            return "Unknown beacon"
        return "Unknown"

    @staticmethod
    def _name_looks_like_wizpr_ring(name_norm: str) -> bool:
        if "case" in name_norm:
            return False
        if "ring" in name_norm and ("wizpr" in name_norm or "wzpr" in name_norm or "whsp" in name_norm):
            return True
        return name_norm.startswith("wizpr ring") or name_norm.startswith("wzpr ring") or name_norm.startswith("whsp ring")


class BLEManager:
    def __init__(self) -> None:
        self._client: BleakClient | None = None
        self._client_address = ""
        self._devices: Dict[str, BLEDevice] = {}
        self._advertisements: Dict[str, AdvertisementData] = {}
        self.on_disconnect: Callable[[str], None] | None = None

    async def adapter_diagnostics(self) -> dict[str, object]:
        try:
            from winrt.windows.devices.bluetooth import BluetoothAdapter
        except Exception as exc:
            return {"error": f"WinRT BluetoothAdapter unavailable: {exc}"}

        try:
            adapter = await BluetoothAdapter.get_default_async()
        except Exception as exc:
            return {"error": f"Could not query Windows BluetoothAdapter: {exc}"}

        if adapter is None:
            return {"adapter": "None"}

        address = int(getattr(adapter, "bluetooth_address", 0) or 0)
        address_hex = f"{address:012X}" if address else ""
        return {
            "address": ":".join(address_hex[i : i + 2] for i in range(0, 12, 2)) if address_hex else "",
            "classic": bool(getattr(adapter, "is_classic_supported", False)),
            "low_energy": bool(getattr(adapter, "is_low_energy_supported", False)),
            "central": bool(getattr(adapter, "is_central_role_supported", False)),
            "peripheral": bool(getattr(adapter, "is_peripheral_role_supported", False)),
        }

    async def bluetooth_radio_diagnostics(self) -> dict[str, Any]:
        try:
            from winrt.windows.devices.radios import Radio, RadioKind
        except Exception as exc:
            return {"available": False, "error": f"WinRT Radios unavailable: {exc}"}

        try:
            radios = await Radio.get_radios_async()
        except Exception as exc:
            return {"available": False, "error": f"Could not query Windows radios: {exc}"}

        bluetooth_radios = []
        for radio in radios:
            if getattr(radio, "kind", None) != RadioKind.BLUETOOTH:
                continue
            state = self._enum_int(getattr(radio, "state", None))
            bluetooth_radios.append(
                {
                    "name": str(getattr(radio, "name", "") or "Bluetooth"),
                    "state": state,
                    "state_name": self._radio_state_name(state),
                }
            )

        return {"available": True, "bluetooth": bluetooth_radios}

    async def ensure_bluetooth_radio_on(self) -> dict[str, Any]:
        try:
            from winrt.windows.devices.radios import Radio, RadioKind, RadioState
        except Exception as exc:
            return {"available": False, "error": f"WinRT Radios unavailable: {exc}"}

        try:
            access = await Radio.request_access_async()
            radios = await Radio.get_radios_async()
        except Exception as exc:
            return {"available": False, "error": f"Could not access Windows radios: {exc}"}

        target_on = self._enum_int(RadioState.ON)
        results: list[dict[str, Any]] = []
        for radio in radios:
            if getattr(radio, "kind", None) != RadioKind.BLUETOOTH:
                continue

            before = self._enum_int(getattr(radio, "state", None))
            entry: dict[str, Any] = {
                "name": str(getattr(radio, "name", "") or "Bluetooth"),
                "before": before,
                "before_name": self._radio_state_name(before),
            }
            if before != target_on:
                try:
                    entry["set_result"] = self._enum_int(await radio.set_state_async(RadioState.ON))
                except Exception as exc:
                    entry["error"] = str(exc)
            after = self._enum_int(getattr(radio, "state", None))
            entry["after"] = after
            entry["after_name"] = self._radio_state_name(after)
            results.append(entry)

        return {
            "available": True,
            "access": self._enum_int(access),
            "radios": results,
            "radio_on": any(item.get("after_name") == "on" for item in results),
            "changed": any(item.get("before") != item.get("after") for item in results),
            "turned_on": any(item.get("before") != target_on and item.get("after") == target_on for item in results),
        }

    async def live_scanner_status(self) -> tuple[bool, str]:
        scanner = BleakScanner()
        try:
            await scanner.start()
        except Exception as exc:
            return False, str(exc)
        finally:
            try:
                await scanner.stop()
            except Exception:
                pass
        return True, ""

    async def health_report(self) -> dict[str, Any]:
        adapter = await self.adapter_diagnostics()
        radio = await self.bluetooth_radio_diagnostics()
        scanner_ok, scanner_error = await self.live_scanner_status()
        associated = await self.windows_associated_devices()
        cached = self.windows_cached_devices()
        known_by_address = {d.address: d for d in cached}
        known_by_address.update({d.address: d for d in associated})
        known = list(known_by_address.values())
        ring_candidates = [d for d in known if d.candidate_label == WIZPR_RING_LABEL]
        case_candidates = [d for d in known if d.candidate_label == "WIZPR case"]
        ble_client_candidates = [d for d in known if d.candidate_label == "WIZPR BLE client"]

        adapter_has_ble_central = (
            adapter.get("low_energy") is True and adapter.get("central") is True
            if "error" not in adapter
            else None
        )
        bluetooth_radio_on = self._bluetooth_radio_on(radio)
        return {
            "adapter": adapter,
            "radio": radio,
            "adapter_has_ble_central": adapter_has_ble_central,
            "bluetooth_radio_on": bluetooth_radio_on,
            "scanner_ok": scanner_ok,
            "scanner_error": scanner_error,
            "associated_devices": associated,
            "cached_devices": cached,
            "ring_candidates": ring_candidates,
            "case_candidates": case_candidates,
            "ble_client_candidates": ble_client_candidates,
            "known_devices": known,
        }

    @staticmethod
    def format_health_report(report: dict[str, Any]) -> str:
        lines: list[str] = ["ble_doctor:"]
        adapter = report.get("adapter", {})
        if isinstance(adapter, dict) and "error" in adapter:
            lines.append(f"  adapter: {adapter['error']}")
        elif isinstance(adapter, dict):
            lines.append(
                "  adapter: "
                f"address={adapter.get('address') or '-'} "
                f"classic={adapter.get('classic')} "
                f"low_energy={adapter.get('low_energy')} "
                f"central={adapter.get('central')} "
                f"peripheral={adapter.get('peripheral')}"
            )
        else:
            lines.append(f"  adapter: {adapter}")

        radio = report.get("radio", {})
        if isinstance(radio, dict) and radio.get("available"):
            bluetooth_radios = radio.get("bluetooth") or []
            if bluetooth_radios:
                states = ", ".join(
                    f"{item.get('name') or 'Bluetooth'}={item.get('state_name') or item.get('state')}"
                    for item in bluetooth_radios
                )
                lines.append(f"  radio: {states}")
            else:
                lines.append("  radio: no Bluetooth radio returned by WinRT")
        elif isinstance(radio, dict) and radio.get("error"):
            lines.append(f"  radio: {radio['error']}")

        if report.get("bluetooth_radio_on") is False:
            lines.append(
                "  blocker: Windows Bluetooth radio is off in WinRT. "
                "Turn Bluetooth on in Windows Settings or run ble_doctor --turn-on-radio."
            )

        if report.get("adapter_has_ble_central") is False:
            lines.append(
                "  blocker: active Windows Bluetooth radio does not expose BLE Central. "
                "WIZPR Ring discovery/control requires BLE Central/GATT."
            )

        if report.get("scanner_ok"):
            lines.append("  live_scanner: ok")
        else:
            err = str(report.get("scanner_error") or "unknown scanner error")
            lines.append(f"  live_scanner: failed: {err}")

        rings = report.get("ring_candidates") or []
        cases = report.get("case_candidates") or []
        lines.append(f"  windows_known_rings: {len(rings)}")
        for dev in rings:
            lines.append(f"    ring: {dev.name or '(no name)'} [{dev.address}]")
        lines.append(f"  windows_known_cases: {len(cases)}")
        for dev in cases:
            lines.append(f"    case: {dev.name or '(no name)'} [{dev.address}]")
        ble_clients = report.get("ble_client_candidates") or []
        lines.append(f"  windows_known_ble_clients: {len(ble_clients)}")
        for dev in ble_clients:
            lines.append(f"    ble_client: {dev.name or '(no name)'} [{dev.address}]")

        if not rings and cases:
            lines.append("  note: WIZPR CASE-* is the charging case, not the ring BLE peripheral.")
        if not rings and ble_clients:
            lines.append("  note: WIZPR BLE client is the app/desktop companion channel, not the ring BLE peripheral.")
        if not rings and report.get("bluetooth_radio_on") is False:
            lines.append("  verdict: blocked before ring protocol; Windows Bluetooth is off for BLE apps.")
        elif not rings and report.get("adapter_has_ble_central") is False:
            lines.append("  verdict: blocked before ring protocol; Windows cannot currently scan/connect BLE Central devices.")
        elif not rings and not report.get("scanner_ok"):
            lines.append("  verdict: live BLE scanning is unavailable; only previously known Windows BLE devices can be listed.")
        elif rings:
            lines.append("  verdict: ring candidate is known to Windows; try ring_probe --address <ring address>.")
        else:
            lines.append("  verdict: BLE adapter looks usable, but no WIZPR ring is known yet.")
        return "\n".join(lines)

    @staticmethod
    def format_radio_repair_result(result: dict[str, Any]) -> str:
        lines = ["bluetooth_radio_repair:"]
        if not result.get("available"):
            lines.append(f"  error: {result.get('error') or 'WinRT Radios unavailable'}")
            return "\n".join(lines)
        lines.append(f"  access: {result.get('access')}")
        for item in result.get("radios") or []:
            line = (
                f"  radio: {item.get('name') or 'Bluetooth'} "
                f"{item.get('before_name') or item.get('before')} -> {item.get('after_name') or item.get('after')}"
            )
            if item.get("set_result") is not None:
                line += f" set_result={item.get('set_result')}"
            if item.get("error"):
                line += f" error={item.get('error')}"
            lines.append(line)
        lines.append(f"  radio_on: {bool(result.get('radio_on'))}")
        lines.append(f"  changed: {bool(result.get('changed'))}")
        return "\n".join(lines)

    def windows_cached_devices(self) -> list[DiscoveredDevice]:
        devices: dict[str, DiscoveredDevice] = {}
        services_by_addr: dict[str, set[str]] = defaultdict(set)

        for key_name in self._registry_subkeys(r"SYSTEM\CurrentControlSet\Enum\BTHLEDEVICE"):
            match = re.match(r"\{([^}]+)\}_([0-9a-fA-F]{12})$", key_name)
            if match:
                services_by_addr[match.group(2).upper()].add(match.group(1).lower())

        for key_name in self._registry_subkeys(r"SYSTEM\CurrentControlSet\Enum\BTHLE"):
            match = re.match(r"Dev_([0-9a-fA-F]{12})$", key_name)
            if not match:
                continue
            addr_hex = match.group(1).upper()
            address = self._format_address(addr_hex)
            name = self._registry_first_value(
                rf"SYSTEM\CurrentControlSet\Enum\BTHLE\{key_name}",
                "FriendlyName",
            )
            self._devices[address] = self._winrt_cached_device(address, str(name or f"Windows BLE cache {address}"))
            devices[address] = DiscoveredDevice(
                address=address,
                name=str(name or f"Windows BLE cache {address}"),
                rssi=0,
                service_uuids=sorted(services_by_addr.get(addr_hex, set())),
            )

        for key_name in self._registry_subkeys(r"SYSTEM\CurrentControlSet\Enum\BTHENUM"):
            match = re.match(r"DEV_([0-9a-fA-F]{12})$", key_name)
            if not match:
                continue
            addr_hex = match.group(1).upper()
            address = self._format_address(addr_hex)
            if address in devices:
                continue
            name = self._registry_first_value(
                rf"SYSTEM\CurrentControlSet\Enum\BTHENUM\{key_name}",
                "FriendlyName",
            )
            if name and self._looks_relevant_name(str(name)):
                self._devices[address] = self._winrt_cached_device(address, str(name))
                devices[address] = DiscoveredDevice(
                    address=address,
                    name=str(name),
                    rssi=0,
                    service_uuids=sorted(services_by_addr.get(addr_hex, set())),
                )

        out = list(devices.values())
        out.sort(key=self._device_sort_key)
        return out

    async def windows_associated_devices(self) -> list[DiscoveredDevice]:
        try:
            from winrt.windows.devices.bluetooth import BluetoothLEDevice
            from winrt.windows.devices.enumeration import DeviceInformation
        except Exception as exc:
            logger.warning("Windows BLE device enumeration unavailable: %s", exc)
            return []

        devices: dict[str, DiscoveredDevice] = {}
        infos_by_id: dict[str, object] = {}
        selectors = []
        for selector_factory in (
            BluetoothLEDevice.get_device_selector,
            lambda: BluetoothLEDevice.get_device_selector_from_pairing_state(True),
            lambda: BluetoothLEDevice.get_device_selector_from_pairing_state(False),
        ):
            try:
                selectors.append(selector_factory())
            except Exception:
                continue

        for selector in selectors:
            try:
                infos = await DeviceInformation.find_all_async_aqs_filter(selector)
            except Exception as exc:
                logger.warning("Windows BLE device enumeration failed: %s", exc)
                continue
            for info in infos:
                infos_by_id[str(getattr(info, "id", ""))] = info

        registry_devices = {d.address: d for d in self.windows_cached_devices()}
        for info in infos_by_id.values():
            address = self._address_from_device_information_id(str(getattr(info, "id", "")))
            if not address:
                continue
            cached = registry_devices.get(address)
            name = str(getattr(info, "name", "") or (cached.name if cached else "") or f"Windows BLE {address}")
            services = sorted(set((cached.service_uuids or []) if cached else []))
            self._devices[address] = self._winrt_cached_device(address, name)
            devices[address] = DiscoveredDevice(
                address=address,
                name=name,
                rssi=0,
                service_uuids=services,
            )

        for address, dev in registry_devices.items():
            devices.setdefault(address, dev)

        out = list(devices.values())
        out.sort(key=self._device_sort_key)
        return out

    async def watch_windows_associated_devices(self, seconds: float, interval: float = 1.0) -> list[DiscoveredDevice]:
        seen: dict[str, DiscoveredDevice] = {}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(seconds, 0.0)

        while True:
            for dev in await self.windows_associated_devices():
                seen[dev.address] = dev
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(max(interval, 0.1), remaining))

        out = list(seen.values())
        out.sort(key=self._device_sort_key)
        return out

    def _remember_advertisement(
        self,
        found: Dict[str, Tuple[BLEDevice, AdvertisementData]],
        device: BLEDevice,
        adv: AdvertisementData,
    ) -> None:
        address = self._normalize_address(device.address)
        found[address] = (device, adv)
        self._devices[address] = device
        self._advertisements[address] = adv

    @staticmethod
    def _to_discovered_device(addr: str, dev: BLEDevice, adv: AdvertisementData) -> DiscoveredDevice:
        name = (adv.local_name or dev.name or "").strip()
        rssi = int(getattr(adv, "rssi", 0) or 0)
        advertised_service_uuids = [str(u) for u in (getattr(adv, "service_uuids", []) or [])]
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
            address=addr,
            name=name,
            rssi=rssi,
            service_uuids=advertised_service_uuids,
            tx_power=int(tx_power) if tx_power is not None else None,
            manufacturer_data=manufacturer_data,
            service_data=service_data,
        )

    def _found_devices(self, found: Dict[str, Tuple[BLEDevice, AdvertisementData]]) -> list[DiscoveredDevice]:
        out = [self._to_discovered_device(addr, dev, adv) for addr, (dev, adv) in found.items()]
        out.sort(key=lambda d: d.rssi, reverse=True)
        return out

    async def scan(self, seconds: float = 5.0, service_uuids: list[str] | None = None) -> list[DiscoveredDevice]:
        found: Dict[str, Tuple[BLEDevice, AdvertisementData]] = {}

        def _cb(device: BLEDevice, adv: AdvertisementData) -> None:
            self._remember_advertisement(found, device, adv)

        scanner = BleakScanner(detection_callback=_cb, service_uuids=service_uuids, scanning_mode="active")
        try:
            await scanner.start()
        except Exception as exc:
            repair = await self.ensure_bluetooth_radio_on()
            if repair.get("radio_on"):
                scanner = BleakScanner(detection_callback=_cb, service_uuids=service_uuids, scanning_mode="active")
                try:
                    await scanner.start()
                except Exception as retry_exc:
                    raise self._scanner_start_error(retry_exc, repair) from retry_exc
            else:
                raise self._scanner_start_error(exc, repair) from exc
        try:
            await asyncio.sleep(seconds)
        finally:
            await scanner.stop()

        out = self._found_devices(found)
        logger.info(
            "BLE scan%s found %d device(s): %s",
            f" services={','.join(service_uuids)}" if service_uuids else "",
            len(out),
            "; ".join(f"{d.name or '(no name)'} [{d.address}] RSSI={d.rssi}" for d in out[:12]),
        )
        return out

    async def scan_wizpr(self, seconds: float = 5.0, include_reverse_ble: bool = False) -> list[DiscoveredDevice]:
        devices = await self.scan(seconds=seconds)
        return self._filter_wizpr_related(devices, include_reverse_ble)

    async def scan_wizpr_live(
        self,
        seconds: float = 20.0,
        preferred_address: str = "",
        prefer_window: float = 3.0,
        include_reverse_ble: bool = False,
    ) -> tuple[list[DiscoveredDevice], DiscoveredDevice | None]:
        found: Dict[str, Tuple[BLEDevice, AdvertisementData]] = {}
        wanted = self._normalize_address(preferred_address).casefold() if preferred_address else ""
        first_ring_seen_at: float | None = None

        def _cb(device: BLEDevice, adv: AdvertisementData) -> None:
            self._remember_advertisement(found, device, adv)

        scanner = BleakScanner(detection_callback=_cb, scanning_mode="active")
        try:
            await scanner.start()
        except Exception as exc:
            repair = await self.ensure_bluetooth_radio_on()
            if repair.get("radio_on"):
                scanner = BleakScanner(detection_callback=_cb, scanning_mode="active")
                try:
                    await scanner.start()
                except Exception as retry_exc:
                    raise self._scanner_start_error(retry_exc, repair) from retry_exc
            else:
                raise self._scanner_start_error(exc, repair) from exc

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(1.0, float(seconds))
        try:
            while True:
                devices = self._filter_wizpr_related(self._found_devices(found), include_reverse_ble)
                rings = [dev for dev in devices if dev.candidate_label == WIZPR_RING_LABEL]
                if rings and first_ring_seen_at is None:
                    first_ring_seen_at = loop.time()

                if wanted:
                    for dev in rings:
                        if dev.address.casefold() == wanted:
                            return devices, dev
                    if rings and first_ring_seen_at is not None and loop.time() - first_ring_seen_at >= max(0.0, prefer_window):
                        return devices, sorted(rings, key=self._device_sort_key)[0]
                elif rings:
                    return devices, sorted(rings, key=self._device_sort_key)[0]

                remaining = deadline - loop.time()
                if remaining <= 0:
                    return devices, None
                await asyncio.sleep(min(0.25, remaining))
        finally:
            await scanner.stop()

    @staticmethod
    def _filter_wizpr_related(devices: list[DiscoveredDevice], include_reverse_ble: bool) -> list[DiscoveredDevice]:
        if include_reverse_ble:
            return [d for d in devices if d.candidate_label in WIZPR_RELATED_LABELS]
        return [d for d in devices if d.candidate_label == WIZPR_RING_LABEL]

    async def find_by_name_prefix(self, prefix: str, timeout: float = 10.0) -> DiscoveredDevice | None:
        prefix_norm = self._norm(prefix)
        try:
            devices = (
                await self.scan_wizpr(seconds=timeout, include_reverse_ble=True)
                if "ring" in prefix_norm
                else await self.scan(seconds=timeout)
            )
        except Exception as exc:
            logger.warning("BLE scan failed while finding %s; watching Windows BLE device lists: %s", prefix, exc)
            devices = await self.watch_windows_associated_devices(timeout)
        matches = [d for d in devices if self._matches_name_prefix(d, prefix_norm)]
        return matches[0] if matches else None

    async def connect(
        self,
        address: str,
        timeout: float = 25.0,
        pair: bool = False,
        lookup: bool = True,
        retry: bool = True,
    ) -> BleakClient:
        await self.disconnect()

        address = self._normalize_address(address)
        device = self._devices.get(address)
        if lookup:
            try:
                device = device or await BleakScanner.find_device_by_address(address, timeout=min(timeout, 12.0))
            except Exception:
                device = None

        if lookup and device is None:
            logger.info("BLE address %s was not in the scan cache; rescanning before connect.", address)
            try:
                await self.scan(seconds=min(timeout, 8.0))
                device = self._devices.get(address)
            except Exception as scan_err:
                logger.warning("BLE rescan before connect failed for %s: %s", address, scan_err)

        if device is None and os.name == "nt":
            device = self._winrt_cached_device(address, name=f"Windows cached BLE {address}")
            logger.info("Using Windows cached BLE address without live scan: %s", address)

        client = BleakClient(device or address, timeout=timeout, pair=pair, disconnected_callback=self._on_disconnected)

        try:
            await client.connect()
        except Exception as first_err:
            try:
                await client.disconnect()
            except Exception:
                pass
            if not retry:
                raise RuntimeError(
                    "Windows could not finish connecting to the remembered WIZPR ring. "
                    "Wake the ring with the button and keep it near the computer, then try Auto Connect again."
                ) from first_err
            try:
                await asyncio.sleep(1.0)
                device2 = await BleakScanner.find_device_by_address(address, timeout=min(timeout, 12.0))
                if device2 is not None:
                    self._devices[address] = device2
                elif os.name == "nt":
                    device2 = self._winrt_cached_device(address, name=f"Windows cached BLE {address}")
                client = BleakClient(device2 or address, timeout=timeout, pair=pair, disconnected_callback=self._on_disconnected)
                await client.connect()
            except Exception as retry_err:
                logger.warning("BLE retry failed for %s: %s", address, retry_err)
                raise RuntimeError(
                    "Windows could not finish connecting to the BLE device. "
                    "For WIZPR, use Auto Connect after the ring is awake, broadcasting, and disconnected from the phone app; pairing is usually not required. "
                    "For non-ring diagnostics, Pair + Connect may help. "
                    "if this repeats, toggle Windows Bluetooth off/on or remove the ring from Windows Bluetooth devices."
                ) from first_err

        if not client.is_connected:
            raise RuntimeError(f"Failed to connect to {address}")

        self._client = client
        self._client_address = address
        logger.info("Connected BLE: %s", address)
        return client

    async def disconnect(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        finally:
            self._client = None
            self._client_address = ""
            logger.info("Disconnected BLE.")

    def _on_disconnected(self, client: BleakClient) -> None:
        if self._client is not client:
            return
        address = self._client_address
        self._client = None
        self._client_address = ""
        logger.info("BLE disconnected: %s", address or "(unknown)")
        if self.on_disconnect is not None:
            try:
                self.on_disconnect(address)
            except Exception:
                logger.exception("BLE disconnect callback failed")

    @property
    def client(self) -> BleakClient | None:
        return self._client

    @staticmethod
    def _norm(value: str) -> str:
        return value.casefold().replace("_", " ").replace("-", " ").strip()

    @classmethod
    def _matches_name_prefix(cls, device: DiscoveredDevice, prefix_norm: str) -> bool:
        name_norm = cls._norm(device.name)
        service_uuids = {u.casefold() for u in (device.service_uuids or [])}
        if "ring" in prefix_norm:
            service_data_keys = {u.casefold() for u in (device.service_data or {}).keys()}
            return (
                name_norm.startswith(prefix_norm)
                or DiscoveredDevice._name_looks_like_wizpr_ring(name_norm)
                or WIZPR_RING_SERVICE_UUID in service_uuids
                or WIZPR_RING_SERVICE_UUID in service_data_keys
            )
        return name_norm.startswith(prefix_norm)

    @staticmethod
    def _device_sort_key(device: DiscoveredDevice) -> tuple[int, int, str, str]:
        label_rank = {
            WIZPR_RING_LABEL: 0,
            "Possible WIZPR/Silicon Labs": 1,
            "WIZPR case": 2,
            "WIZPR BLE client": 3,
        }.get(device.candidate_label, 4)
        return (label_rank, -int(device.rssi or 0), device.name.casefold(), device.address)

    @staticmethod
    def _format_address(addr_hex: str) -> str:
        clean = re.sub(r"[^0-9a-fA-F]", "", addr_hex).upper()
        return ":".join(clean[i : i + 2] for i in range(0, 12, 2))

    @classmethod
    def _normalize_address(cls, address: str) -> str:
        clean = re.sub(r"[^0-9a-fA-F]", "", address)
        if len(clean) == 12:
            return cls._format_address(clean)
        return address.strip()

    @classmethod
    def _address_to_int(cls, address: str) -> int:
        clean = re.sub(r"[^0-9a-fA-F]", "", address)
        if len(clean) != 12:
            raise ValueError(f"Invalid BLE address: {address}")
        return int(clean, 16)

    @classmethod
    def _winrt_cached_device(cls, address: str, name: str | None = None) -> BLEDevice:
        if RawAdvData is None:
            raise RuntimeError("Windows BLE backend is unavailable")
        details = RawAdvData(SimpleNamespace(bluetooth_address=cls._address_to_int(address)), None)
        return BLEDevice(cls._normalize_address(address), name, details)

    @classmethod
    def _address_from_device_information_id(cls, device_id: str) -> str:
        match = re.search(r"-([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})(?:$|[\\#])", device_id)
        if match:
            return cls._normalize_address(match.group(1))
        match = re.search(r"DEV_([0-9a-fA-F]{12})", device_id, re.IGNORECASE)
        if match:
            return cls._format_address(match.group(1))
        return ""

    @classmethod
    def _looks_relevant_name(cls, name: str) -> bool:
        norm = cls._norm(name)
        return any(token in norm for token in ("wizpr", "wzpr", "whsp", "ring", "iotbt"))

    @staticmethod
    def _enum_int(value: object) -> int | None:
        try:
            return int(value)  # WinRT enums behave like ints, but keep this defensive.
        except Exception:
            return None

    @staticmethod
    def _radio_state_name(value: int | None) -> str:
        return {
            0: "unknown",
            1: "on",
            2: "off",
            3: "disabled",
        }.get(value, str(value or "unknown"))

    @classmethod
    def _bluetooth_radio_on(cls, radio: dict[str, Any]) -> bool | None:
        if not isinstance(radio, dict) or not radio.get("available"):
            return None
        bluetooth_radios = radio.get("bluetooth") or []
        if not bluetooth_radios:
            return None
        return any(item.get("state_name") == "on" for item in bluetooth_radios)

    @classmethod
    def _scanner_start_error(cls, exc: Exception, repair: dict[str, Any] | None = None) -> RuntimeError:
        repair_hint = ""
        if repair:
            radios = repair.get("radios") or []
            states = ", ".join(
                f"{item.get('name') or 'Bluetooth'}={item.get('after_name') or item.get('after')}"
                for item in radios
            )
            if states:
                repair_hint = f" Windows radio state after repair attempt: {states}."
            elif repair.get("error"):
                repair_hint = f" Radio repair unavailable: {repair.get('error')}."

        return RuntimeError(
            "Windows BLE scanning could not start. The active Bluetooth radio must be on and expose Bluetooth LE "
            "scanning to Windows; classic Bluetooth audio/pairing can work even when BLE scanning does not. "
            "If you are using a USB adapter, unplug/replug it and confirm Device Manager shows a present "
            "Microsoft Bluetooth LE Enumerator for that stack."
            f"{repair_hint}"
        )

    @staticmethod
    def _registry_subkeys(path: str) -> list[str]:
        if winreg is None:
            return []
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
        except OSError:
            return []
        out: list[str] = []
        try:
            count = winreg.QueryInfoKey(key)[0]
            for idx in range(count):
                try:
                    out.append(winreg.EnumKey(key, idx))
                except OSError:
                    break
        finally:
            winreg.CloseKey(key)
        return out

    @classmethod
    def _registry_first_value(cls, path: str, value_name: str) -> object | None:
        if winreg is None:
            return None
        stack = [path]
        while stack:
            current = stack.pop()
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, current)
            except OSError:
                continue
            try:
                try:
                    return winreg.QueryValueEx(key, value_name)[0]
                except OSError:
                    pass
                count = winreg.QueryInfoKey(key)[0]
                for idx in range(count - 1, -1, -1):
                    try:
                        stack.append(current + "\\" + winreg.EnumKey(key, idx))
                    except OSError:
                        continue
            finally:
                winreg.CloseKey(key)
        return None
