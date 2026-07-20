from __future__ import annotations

import asyncio
import json
import math
import time
import wave
from array import array
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict

from ..core.event_bus import EventBus
from ..core.logging_setup import get_logger
from .ble_manager import BLEManager

logger = get_logger("wizpr_suite.ring")

WIZPR_DEVICE_NAME_PREFIX = "WIZPR RING"
WIZPR_SERVICE_UUID = "00000000-dc2e-4362-93d3-df429eb3ad10"
WIZPR_AUDIO_CHAR_UUID = "00000001-dc2e-4362-93d3-df429eb3ad10"
WIZPR_MIC_STATE_CHAR_UUID = "00000005-dc2e-4362-93d3-df429eb3ad10"
WIZPR_TRANSFER_STATUS_CHAR_UUID = WIZPR_MIC_STATE_CHAR_UUID
WIZPR_COMMAND_CHAR_UUID = "00000007-dc2e-4362-93d3-df429eb3ad10"
WIZPR_SAMPLE_RATE_COMMAND = "sample_rate 16"
WIZPR_BATTERY_COMMAND = "BATTERY"
WIZPR_TRANSFER_START = ord("1")
WIZPR_TRANSFER_STOP = ord("0")
WIZPR_AUDIO_SAMPLE_RATE = 16000
WIZPR_AUDIO_FINALIZE_DELAY_SECONDS = 0.18
WIZPR_AUDIO_IDLE_FINALIZE_DELAY_SECONDS = 0.65
WIZPR_MIC_SIGNAL_DEBOUNCE_SECONDS = 0.22
WIZPR_AUDIO_MAX_CAPTURE_SECONDS = 45.0
WIZPR_RECORDING_ANNOUNCE_MIN_PACKETS = 8
WIZPR_SILENT_CAPTURE_ABORT_SECONDS = 1.35
WIZPR_SILENT_CAPTURE_REARM_SECONDS = 0.85
WIZPR_DOUBLE_CLICK_WINDOW_SECONDS = 1.0
WIZPR_MIN_AUDIO_CAPTURE_PACKETS = 8
WIZPR_MIN_AUDIO_CAPTURE_SECONDS = 0.20
_WIZPR_IMA_INDEX_TABLE = (-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8)
_WIZPR_IMA_STEP_TABLE = (
    7,
    8,
    9,
    10,
    11,
    12,
    13,
    14,
    16,
    17,
    19,
    21,
    23,
    25,
    28,
    31,
    34,
    37,
    41,
    45,
    50,
    55,
    60,
    66,
    73,
    80,
    88,
    97,
    107,
    118,
    130,
    143,
    157,
    173,
    190,
    209,
    230,
    253,
    279,
    307,
    337,
    371,
    408,
    449,
    494,
    544,
    598,
    658,
    724,
    796,
    876,
    963,
    1060,
    1166,
    1282,
    1411,
    1552,
    1707,
    1878,
    2066,
    2272,
    2499,
    2749,
    3024,
    3327,
    3660,
    4026,
    4428,
    4871,
    5358,
    5894,
    6484,
    7132,
    7845,
    8630,
    9493,
    10442,
    11487,
    12635,
    13899,
    15289,
    16818,
    18500,
    20350,
    22385,
    24623,
    27086,
    29794,
    32767,
)
_WIZPR_BATTERY_VOLTAGE_TABLE = (
    (3.740, 100),
    (3.730, 97),
    (3.720, 95),
    (3.710, 94),
    (3.700, 91),
    (3.690, 90),
    (3.680, 87),
    (3.670, 85),
    (3.660, 84),
    (3.650, 81),
    (3.640, 78),
    (3.630, 77),
    (3.620, 75),
    (3.610, 72),
    (3.600, 71),
    (3.590, 68),
    (3.580, 67),
    (3.570, 65),
    (3.560, 62),
    (3.550, 61),
    (3.540, 58),
    (3.530, 56),
    (3.520, 54),
    (3.510, 52),
    (3.500, 49),
    (3.490, 48),
    (3.480, 46),
    (3.470, 44),
    (3.460, 42),
    (3.450, 39),
    (3.440, 38),
    (3.430, 37),
    (3.420, 35),
    (3.410, 34),
    (3.400, 32),
    (3.390, 30),
    (3.380, 29),
    (3.370, 28),
    (3.360, 27),
    (3.350, 25),
    (3.340, 24),
    (3.330, 22),
    (3.320, 19),
    (3.310, 16),
    (3.300, 15),
    (3.290, 13),
    (3.280, 13),
    (3.270, 11),
    (3.260, 11),
    (3.250, 11),
    (3.240, 10),
    (3.230, 10),
    (3.220, 10),
    (3.210, 10),
    (3.200, 9),
    (3.190, 9),
    (3.180, 9),
    (3.170, 9),
    (3.160, 8),
    (3.150, 8),
    (3.140, 8),
    (3.130, 8),
    (3.120, 6),
    (3.110, 6),
    (3.100, 6),
    (3.090, 6),
    (3.080, 5),
    (3.070, 5),
    (3.060, 5),
    (3.050, 5),
    (3.040, 4),
    (3.030, 4),
    (3.020, 4),
    (3.010, 4),
    (3.000, 4),
    (2.990, 4),
    (2.980, 3),
    (2.970, 3),
    (2.960, 3),
    (2.950, 3),
    (2.940, 1),
    (2.930, 1),
    (2.920, 1),
    (2.910, 1),
    (2.900, 0),
)


@dataclass
class RingProfile:
    address: str = ""


@dataclass
class WizprChannels:
    command: str = ""
    audio: str = ""
    mic_state: str = ""
    notify_fallback: list[str] = field(default_factory=list)


def parse_battery_update(text: str) -> tuple[float, int] | None:
    voltage = _parse_battery_voltage(text)
    if voltage is None:
        return None
    return voltage, battery_voltage_to_percent(voltage)


def click_topic_for_count(count: int) -> str:
    return {
        1: "button_single",
        2: "button_double",
        3: "button_triple",
        4: "button_quad",
        5: "button_five",
    }.get(count, "button_multi")


def battery_voltage_to_percent(voltage: float) -> int:
    if voltage <= 0.0:
        return 0

    max_voltage, max_percent = _WIZPR_BATTERY_VOLTAGE_TABLE[0]
    if voltage >= max_voltage:
        return max_percent

    min_voltage, min_percent = _WIZPR_BATTERY_VOLTAGE_TABLE[-1]
    if voltage <= min_voltage:
        return min_percent

    for current, next_item in zip(_WIZPR_BATTERY_VOLTAGE_TABLE, _WIZPR_BATTERY_VOLTAGE_TABLE[1:]):
        current_voltage, current_percent = current
        next_voltage, next_percent = next_item
        if next_voltage <= voltage <= current_voltage:
            voltage_range = current_voltage - next_voltage
            percent_range = current_percent - next_percent
            offset = current_voltage - voltage
            return int(round(current_percent - (offset / voltage_range) * percent_range))

    return 50


def _parse_battery_voltage(text: str) -> float | None:
    if "BATT=" in text:
        _, tail = text.split("BATT=", 1)
        return _parse_float_prefix(tail)

    if "BATTERY" in text:
        open_idx = text.find("(")
        close_idx = text.find(")", open_idx + 1)
        if open_idx >= 0 and close_idx > open_idx:
            try:
                return float(text[open_idx + 1 : close_idx].strip())
            except ValueError:
                return None

    return None


def _parse_float_prefix(text: str) -> float | None:
    value = text.strip()
    end = 0
    for end, ch in enumerate(value):
        if not (ch.isdigit() or ch in ".-+"):
            break
    else:
        end = len(value)
    if end == 0:
        return None
    try:
        return float(value[:end])
    except ValueError:
        return None


@dataclass
class RingAudioSnapshot:
    packets: list[bytes]
    started_at: str
    capture_serial: int


def _pcm_activity_metrics(pcm: bytes, sample_rate: int = WIZPR_AUDIO_SAMPLE_RATE) -> dict[str, float]:
    if not pcm or sample_rate <= 0:
        return {
            "duration_seconds": 0.0,
            "rms": 0.0,
            "peak": 0.0,
            "active_seconds": 0.0,
            "active_run_seconds": 0.0,
            "active_ratio": 0.0,
        }

    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return {
            "duration_seconds": 0.0,
            "rms": 0.0,
            "peak": 0.0,
            "active_seconds": 0.0,
            "active_run_seconds": 0.0,
            "active_ratio": 0.0,
        }

    scale = 32768.0
    duration = len(samples) / float(sample_rate)
    mean = sum(samples) / float(len(samples))
    centered = [float(value) - mean for value in samples]
    rms = math.sqrt(sum(value * value for value in centered) / float(len(centered))) / scale
    peak = max(abs(value) for value in centered) / scale

    frame = max(1, int(sample_rate * 0.03))
    hop = max(1, int(sample_rate * 0.01))
    frame_rms: list[float] = []
    if len(centered) < frame:
        frame_rms.append(rms)
    else:
        for start in range(0, len(centered) - frame + 1, hop):
            chunk = centered[start : start + frame]
            value = math.sqrt(sum(sample * sample for sample in chunk) / float(frame)) / scale
            frame_rms.append(value)

    sorted_rms = sorted(frame_rms)
    noise_floor = sorted_rms[max(0, int(len(sorted_rms) * 0.20) - 1)] if sorted_rms else 0.0
    threshold = max(0.0010, noise_floor * 2.8, rms * 0.38)
    active = [value >= threshold for value in frame_rms]
    active_count = sum(1 for value in active if value)
    longest = 0
    current = 0
    for value in active:
        if value:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    hop_seconds = hop / float(sample_rate)
    return {
        "duration_seconds": duration,
        "rms": rms,
        "peak": peak,
        "active_seconds": active_count * hop_seconds,
        "active_run_seconds": longest * hop_seconds,
        "active_ratio": active_count / float(len(active)) if active else 0.0,
    }


def _metrics_have_speech(metrics: dict[str, float]) -> bool:
    duration = float(metrics.get("duration_seconds", 0.0))
    rms = float(metrics.get("rms", 0.0))
    peak = float(metrics.get("peak", 0.0))
    active = float(metrics.get("active_seconds", 0.0))
    active_run = float(metrics.get("active_run_seconds", 0.0))
    active_ratio = float(metrics.get("active_ratio", 0.0))
    if duration < 0.16 or rms < 0.0010 or peak < 0.005:
        return False
    if active_run >= 0.10 and active >= 0.14 and active_ratio >= 0.08:
        return True
    return rms >= 0.0030 and peak >= 0.015 and active >= 0.10


class RingAudioCapture:
    def __init__(
        self,
        capture_dir: Path,
        min_packets: int = WIZPR_MIN_AUDIO_CAPTURE_PACKETS,
        min_seconds: float = WIZPR_MIN_AUDIO_CAPTURE_SECONDS,
    ) -> None:
        self.capture_dir = capture_dir
        self.min_packets = max(1, int(min_packets))
        self.min_seconds = max(0.0, float(min_seconds))
        self._packets: list[bytes] = []
        self.active = False
        self._capture_serial = 0
        self._started_at = ""

    def start(self) -> None:
        self._packets = []
        self.active = True
        self._started_at = datetime.now().isoformat(timespec="milliseconds")

    def feed(self, packet: bytes) -> None:
        if self.active and packet:
            self._packets.append(bytes(packet))

    @property
    def packet_count(self) -> int:
        return len(self._packets)

    def activity_metrics(self) -> dict[str, float]:
        pcm, _stats = self._decode_packets_with_stats(self._packets)
        return _pcm_activity_metrics(pcm)

    def has_speech_like_activity(self) -> bool:
        return _metrics_have_speech(self.activity_metrics())

    def discard(self) -> None:
        self.active = False
        self._packets = []

    def detach(self) -> RingAudioSnapshot | None:
        self.active = False
        if not self._packets:
            self._packets = []
            return None
        self._capture_serial += 1
        snapshot = RingAudioSnapshot(
            packets=self._packets,
            started_at=self._started_at,
            capture_serial=self._capture_serial,
        )
        self._packets = []
        return snapshot

    def stop_and_save(self) -> Path | None:
        snapshot = self.detach()
        if snapshot is None:
            return None
        return self.save_snapshot(snapshot)

    def save_snapshot(self, snapshot: RingAudioSnapshot) -> Path | None:
        packet_count = len(snapshot.packets)
        packet_bytes = sum(len(packet) for packet in snapshot.packets)
        pcm, packet_stats = self._decode_packets_with_stats(snapshot.packets)
        if packet_count < self.min_packets:
            logger.info("Ignored short WIZPR audio capture: packets=%d", packet_count)
            return None
        if not pcm:
            return None
        duration = len(pcm) / float(WIZPR_AUDIO_SAMPLE_RATE * 2)
        if duration < self.min_seconds:
            logger.info("Ignored short WIZPR audio capture: packets=%d duration=%.2fs", packet_count, duration)
            return None

        self.capture_dir.mkdir(parents=True, exist_ok=True)
        path = self._next_capture_path(snapshot.capture_serial)
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(WIZPR_AUDIO_SAMPLE_RATE)
            wf.writeframes(pcm)
        self._write_capture_metadata(
            path,
            started_at=snapshot.started_at,
            packet_count=packet_count,
            packet_bytes=packet_bytes,
            duration_seconds=duration,
            pcm_bytes=len(pcm),
            packet_stats=packet_stats,
        )
        logger.info(
            "Saved WIZPR audio capture: %s packets=%d duration=%.2fs duplicates=%d gaps=%d",
            path,
            packet_count,
            duration,
            packet_stats["duplicate_packets"],
            packet_stats["missing_packets"],
        )
        return path

    def _next_capture_path(self, serial: int) -> Path:
        stem = f"wizpr_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]}"
        path = self.capture_dir / f"{stem}.wav"
        if not path.exists():
            return path
        suffix = max(1, int(serial))
        while True:
            path = self.capture_dir / f"{stem}_{suffix:02d}.wav"
            if not path.exists():
                return path
            suffix += 1

    def _write_capture_metadata(
        self,
        path: Path,
        *,
        started_at: str,
        packet_count: int,
        packet_bytes: int,
        duration_seconds: float,
        pcm_bytes: int,
        packet_stats: dict[str, int],
    ) -> None:
        metadata = {
            "source": "WIZPR Ring",
            "sdk_style": "decoded 16 kHz mono PCM WAV",
            "wav": path.name,
            "started_at": started_at,
            "saved_at": datetime.now().isoformat(timespec="milliseconds"),
            "sample_rate": WIZPR_AUDIO_SAMPLE_RATE,
            "channels": 1,
            "sample_width_bytes": 2,
            "packet_count": packet_count,
            "packet_bytes": packet_bytes,
            "pcm_bytes": pcm_bytes,
            "duration_seconds": round(float(duration_seconds), 3),
            "format": "wav/pcm16",
            **packet_stats,
        }
        try:
            path.with_suffix(".json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Could not write WIZPR capture metadata for %s: %s", path, exc)

    def _decode_packets(self, packets: list[bytes]) -> bytes:
        pcm, _stats = self._decode_packets_with_stats(packets)
        return pcm

    def _decode_packets_with_stats(self, packets: list[bytes]) -> tuple[bytes, dict[str, int]]:
        audio_packets = [packet for packet in packets if packet and not self._looks_like_text_packet(packet)]
        stats = {
            "sequence_prefix_bytes": 0,
            "duplicate_packets": 0,
            "missing_packets": 0,
            "out_of_order_packets": 0,
            "decoded_packets": 0,
        }
        if not audio_packets:
            return b"", stats

        prefix_len = self._sequence_prefix_len(audio_packets)
        stats["sequence_prefix_bytes"] = prefix_len
        if prefix_len:
            audio_packets, stream_stats = self._normalize_sequenced_packets(audio_packets, prefix_len)
            stats.update(stream_stats)

        last = 0
        step_index = 0
        chunks: list[bytes] = []
        for packet in audio_packets:
            pcm, last, step_index = self._decode_wizpr_ima_packet(packet, last, step_index)
            chunks.append(pcm)
        stats["decoded_packets"] = len(audio_packets)
        return b"".join(chunks), stats

    @staticmethod
    def _normalize_sequenced_packets(
        packets: list[bytes],
        prefix_len: int,
    ) -> tuple[list[bytes], dict[str, int]]:
        modulus = 1 << (8 * prefix_len)
        half_range = modulus // 2
        ordered: dict[int, bytes] = {}
        duplicate_packets = 0
        out_of_order_packets = 0
        first_sequence: int | None = None
        previous_offset: int | None = None

        for packet in packets:
            if len(packet) <= prefix_len:
                continue
            sequence = int.from_bytes(packet[:prefix_len], "big", signed=False)
            payload = packet[prefix_len:]
            if first_sequence is None:
                first_sequence = sequence
            offset = (sequence - first_sequence) % modulus
            if offset >= half_range:
                out_of_order_packets += 1
                continue
            if offset in ordered:
                duplicate_packets += 1
                continue
            if previous_offset is not None and offset < previous_offset:
                out_of_order_packets += 1
            ordered[offset] = payload
            previous_offset = offset

        offsets = sorted(ordered)
        missing_packets = 0
        for before, after in zip(offsets, offsets[1:]):
            if after > before + 1:
                missing_packets += after - before - 1
        normalized = [ordered[offset] for offset in offsets]
        return normalized, {
            "sequence_prefix_bytes": prefix_len,
            "duplicate_packets": duplicate_packets,
            "missing_packets": missing_packets,
            "out_of_order_packets": out_of_order_packets,
            "decoded_packets": len(normalized),
        }

    @staticmethod
    def _decode_wizpr_ima_packet(packet: bytes, last: int, step_index: int) -> tuple[bytes, int, int]:
        pcm = bytearray(len(packet) * 4)
        out = 0
        for byte in packet:
            for code in ((byte >> 4) & 0x0F, byte & 0x0F):
                step = _WIZPR_IMA_STEP_TABLE[step_index]
                diff = step >> 3
                if code & 0x01:
                    diff += step >> 2
                if code & 0x02:
                    diff += step >> 1
                if code & 0x04:
                    diff += step
                if code & 0x08:
                    diff = -diff
                last = max(-32768, min(32767, last + diff))
                step_index = max(0, min(88, step_index + _WIZPR_IMA_INDEX_TABLE[code & 0x0F]))
                pcm[out : out + 2] = int(last).to_bytes(2, "little", signed=True)
                out += 2
        return bytes(pcm), last, step_index

    @staticmethod
    def _sequence_prefix_len(packets: list[bytes]) -> int:
        candidates = [packet for packet in packets if len(packet) > 8]
        if len(candidates) < 6:
            return 0
        seqs = [int.from_bytes(packet[:2], "big", signed=False) for packet in candidates]
        deltas = [(after - before) % 65536 for before, after in zip(seqs, seqs[1:])]
        forward = sum(1 for delta in deltas if 1 <= delta <= 8)
        plausible = sum(1 for delta in deltas if 0 <= delta <= 8 or delta >= 65528)
        unique = len(set(seqs))
        if unique >= 4 and forward >= max(3, int(len(deltas) * 0.55)) and plausible >= int(len(deltas) * 0.80):
            return 2
        return 0

    @staticmethod
    def _looks_like_text_packet(packet: bytes) -> bool:
        if len(packet) > 96:
            return False
        try:
            text = packet.decode("ascii").strip().upper()
        except UnicodeDecodeError:
            return False
        if not text:
            return False
        control_prefixes = (
            "BATT",
            "BATTERY",
            "CLICK",
            "DOUBLE_CLICK",
            "EVT:",
            "EVENT",
            "LOCK",
            "MIC_",
            "POWER_OFF",
            "PROXY",
            "SLEEP",
            "VER ",
            "VERSION",
            "VIDLE",
        )
        return text.startswith(control_prefixes)


class RingController:
    """
    WIZPR Ring BLE controller.

    The shipping ring exposes a custom control service with ASCII events and
    commands on char 00000007, plus 16 kHz mono IMA ADPCM audio on char
    00000001 while the mic is active.
    """

    def __init__(
        self,
        ble: BLEManager,
        bus: EventBus,
        profile: RingProfile,
        capture_dir: Path | None = None,
        audio_finalize_delay: float = WIZPR_AUDIO_FINALIZE_DELAY_SECONDS,
        audio_idle_finalize_delay: float | None = None,
    ) -> None:
        self.ble = ble
        self.bus = bus
        self.profile = profile
        self.audio = RingAudioCapture(capture_dir or Path.cwd() / "captures")
        self.audio_finalize_delay = max(0.0, float(audio_finalize_delay))
        if audio_idle_finalize_delay is None:
            audio_idle_finalize_delay = max(self.audio_finalize_delay, WIZPR_AUDIO_IDLE_FINALIZE_DELAY_SECONDS)
        self.audio_idle_finalize_delay = max(0.0, float(audio_idle_finalize_delay))
        self._notify_handlers: Dict[str, Callable[[int, bytearray], None]] = {}
        self._channels = WizprChannels()
        self._click_count = 0
        self._click_task: asyncio.Task[None] | None = None
        self._audio_generation = 0
        self._audio_finalize_task: asyncio.Task[None] | None = None
        self._audio_watchdog_task: asyncio.Task[None] | None = None
        self._audio_silence_task: asyncio.Task[None] | None = None
        self._mic_signal_active = False
        self._suppress_audio_until_mic_release = False
        self._last_silent_abort_at = 0.0
        self._mic_announced = False
        self._last_mic_value: int | None = None
        self._last_mic_event_at = 0.0
        self._last_audio_packet_at = 0.0
        self._audio_session_id = 0
        self._event_tasks: set[asyncio.Task[Any]] = set()

    async def connect(
        self,
        pair: bool = False,
        timeout: float = 25.0,
        lookup: bool = True,
        retry: bool = True,
    ) -> None:
        if not self.profile.address:
            raise RuntimeError("No BLE address set.")
        await self.ble.connect(self.profile.address, pair=pair, timeout=timeout, lookup=lookup, retry=retry)

    async def disconnect(self) -> None:
        self._cancel_audio_finalize()
        self._cancel_audio_watchdog()
        self._cancel_audio_silence_check()
        self.audio.discard()
        tasks = list(self._event_tasks)
        self._event_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.ble.disconnect()

    def _spawn(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self._event_tasks.add(task)
        task.add_done_callback(self._event_tasks.discard)
        return task

    async def gatt_summary(self) -> list[dict[str, Any]]:
        client = self._require_client()
        services = client.services
        out: list[dict[str, Any]] = []
        for s in services:
            sdict: dict[str, Any] = {
                "uuid": str(s.uuid),
                "description": str(getattr(s, "description", "")),
                "characteristics": [],
            }
            for c in s.characteristics:
                sdict["characteristics"].append(
                    {
                        "uuid": str(c.uuid),
                        "properties": list(getattr(c, "properties", []) or []),
                        "description": str(getattr(c, "description", "")),
                    }
                )
            out.append(sdict)
        return out

    async def subscribe(self, char_uuid: str) -> None:
        client = self._require_client()

        async def _publish(payload: dict[str, Any]) -> None:
            await self.bus.publish("raw_notify", payload)

        def _cb(_sender: int, data: bytearray) -> None:
            text = self._decode_text(data)
            self._spawn(
                _publish(
                    {
                        "uuid": char_uuid,
                        "data_hex": data.hex(),
                        "text": text,
                    }
                )
            )
            self._publish_basic_text_event(char_uuid, text)

        await client.start_notify(char_uuid, _cb)
        self._notify_handlers[char_uuid] = _cb
        logger.info("Subscribed notify: %s", char_uuid)

    async def subscribe_wizpr_channels(self, strict: bool = True, include_fallback: bool = False) -> None:
        self._channels = self.resolve_wizpr_channels(strict=strict)
        subscribed: list[str] = []

        if self._channels.command:
            if await self._start_notify(self._channels.command, self._on_command_notify):
                subscribed.append(f"command={self._channels.command}")
        if self._channels.audio and self._channels.audio != self._channels.command:
            if await self._start_notify(self._channels.audio, self._on_audio_notify):
                subscribed.append(f"audio={self._channels.audio}")
        if self._channels.mic_state and self._channels.mic_state not in (self._channels.command, self._channels.audio):
            if await self._start_notify(self._channels.mic_state, self._on_mic_state_notify):
                subscribed.append(f"mic_state={self._channels.mic_state}")

        if include_fallback:
            for uuid in self._channels.notify_fallback:
                if uuid not in self._notify_handlers and await self._start_notify(uuid, self._on_generic_notify):
                    subscribed.append(f"notify={uuid}")

        if subscribed:
            logger.info("Subscribed BLE channels: %s", ", ".join(subscribed))
        else:
            logger.warning("No notify channels found on the connected BLE device.")

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

    async def write_command(self, command: str, response: bool = True) -> None:
        client = self._require_client()
        char_uuid = self._channels.command or self.resolve_wizpr_channels(strict=False).command
        if not char_uuid:
            raise RuntimeError("No writable WIZPR command characteristic found.")
        data = command.strip().encode("ascii")
        try:
            await client.write_gatt_char(char_uuid, data, response=response)
        except Exception as exc:
            if not response:
                raise
            logger.warning("WIZPR command write-with-response failed; retrying without response: %s", exc)
            await client.write_gatt_char(char_uuid, data, response=False)
        await self.bus.publish("ring_command", {"command": command.strip().upper()})

    async def configure_basic_settings(self) -> None:
        await self.set_sample_rate_16()
        await self.query_battery()

    async def start_wizpr_session(self) -> None:
        if not self.has_wizpr_signature():
            raise RuntimeError(
                "Connected device does not expose the WIZPR ring BLE service "
                f"({WIZPR_SERVICE_UUID}) or known WIZPR characteristics. It is probably not the ring."
            )
        await self.subscribe_wizpr_channels(strict=True, include_fallback=False)
        await self.configure_basic_settings()

    async def set_sample_rate_16(self) -> None:
        await self.write_command(WIZPR_SAMPLE_RATE_COMMAND)

    async def query_battery(self) -> None:
        await self.write_command(WIZPR_BATTERY_COMMAND)

    async def query_version(self) -> None:
        await self.write_command("GET_VERSION")

    async def lock(self) -> None:
        await self.write_command("LOCK")
        await self.bus.publish("lock", {"text": "LOCK", "source": "command"})

    async def sleep(self) -> None:
        await self.write_command("SLEEP")
        await self.bus.publish("sleep", {"text": "SLEEP", "source": "command"})

    async def query_proxy(self) -> None:
        await self.write_command("GET_PROXY")

    def _require_client(self):
        client = self.ble.client
        if client is None or not client.is_connected:
            raise RuntimeError("Not connected")
        return client

    def has_wizpr_signature(self) -> bool:
        client = self._require_client()
        service_uuids = {str(getattr(s, "uuid", "")).lower() for s in client.services}
        char_uuids = {
            str(getattr(c, "uuid", "")).lower()
            for s in client.services
            for c in (getattr(s, "characteristics", []) or [])
        }
        return (
            WIZPR_SERVICE_UUID in service_uuids
            or WIZPR_COMMAND_CHAR_UUID in char_uuids
            or WIZPR_AUDIO_CHAR_UUID in char_uuids
            or WIZPR_MIC_STATE_CHAR_UUID in char_uuids
        )

    def resolve_wizpr_channels(self, strict: bool = True) -> WizprChannels:
        client = self._require_client()
        services = list(client.services)
        service_uuids = {str(getattr(s, "uuid", "")).lower() for s in services}
        has_ring_service = WIZPR_SERVICE_UUID in service_uuids

        chars: list[Any] = []
        ring_chars: list[Any] = []
        for service in services:
            service_chars = list(getattr(service, "characteristics", []) or [])
            chars.extend(list(getattr(service, "characteristics", []) or []))
            if str(getattr(service, "uuid", "")).lower() == WIZPR_SERVICE_UUID:
                ring_chars.extend(service_chars)

        def props(char: Any) -> set[str]:
            return {str(p).lower() for p in (getattr(char, "properties", []) or [])}

        def uuid(char: Any) -> str:
            return str(getattr(char, "uuid", ""))

        def can_notify(char: Any) -> bool:
            p = props(char)
            return "notify" in p or "indicate" in p

        def can_write(char: Any) -> bool:
            p = props(char)
            return "write" in p or "write-without-response" in p

        def exact(target: str) -> Any | None:
            target = target.lower()
            return next((c for c in chars if uuid(c).lower() == target), None)

        command = exact(WIZPR_COMMAND_CHAR_UUID)
        audio = exact(WIZPR_AUDIO_CHAR_UUID)
        mic_state = exact(WIZPR_MIC_STATE_CHAR_UUID)

        has_exact_wizpr_char = command is not None or audio is not None or mic_state is not None
        if not has_ring_service and not has_exact_wizpr_char:
            raise RuntimeError(
                "Connected device does not expose the WIZPR ring BLE service "
                f"({WIZPR_SERVICE_UUID}) or known WIZPR characteristics. It is probably not the ring."
            )

        if strict:
            missing = []
            if audio is None:
                missing.append(f"audio {WIZPR_AUDIO_CHAR_UUID}")
            if mic_state is None:
                missing.append(f"transfer status {WIZPR_TRANSFER_STATUS_CHAR_UUID}")
            if command is None:
                missing.append(f"operation {WIZPR_COMMAND_CHAR_UUID}")
            if missing:
                raise RuntimeError(
                    "Connected WIZPR device is missing the official SDK characteristic(s): "
                    + ", ".join(missing)
                    + ". Open Advanced diagnostics if you want to inspect this device anyway."
                )

        candidate_chars = ring_chars if has_ring_service else chars
        if command is None and has_ring_service:
            command = next((c for c in candidate_chars if can_write(c) and can_notify(c)), None)
        if command is None and has_ring_service:
            command = next((c for c in candidate_chars if can_write(c)), None)

        selected = {uuid(c) for c in (command, audio, mic_state) if c is not None}
        fallback = [uuid(c) for c in candidate_chars if can_notify(c) and uuid(c) not in selected]
        channels = WizprChannels(
            command=uuid(command) if command is not None else "",
            audio=uuid(audio) if audio is not None else "",
            mic_state=uuid(mic_state) if mic_state is not None else "",
            notify_fallback=fallback,
        )
        logger.info("Resolved WIZPR channels: %s", channels)
        return channels

    async def _start_notify(self, char_uuid: str, callback: Callable[[int, bytearray], None]) -> bool:
        client = self._require_client()
        if char_uuid in self._notify_handlers:
            return True
        try:
            await client.start_notify(char_uuid, callback)
        except Exception as exc:
            logger.warning("Could not subscribe %s: %s", char_uuid, exc)
            return False
        self._notify_handlers[char_uuid] = callback
        return True

    def _on_command_notify(self, _sender: int, data: bytearray) -> None:
        text = self._decode_text(data)
        self._publish_raw(self._channels.command or WIZPR_COMMAND_CHAR_UUID, data, text)
        self._handle_text_event(text, self._channels.command or WIZPR_COMMAND_CHAR_UUID)

    def _on_generic_notify(self, sender: int, data: bytearray) -> None:
        sender_uuid = self._sender_uuid(sender)
        text = self._decode_text(data)
        self._publish_raw(sender_uuid, data, text)
        if self._looks_like_text(text, data):
            self._handle_text_event(text, sender_uuid)
        elif self.audio.active:
            self.audio.feed(bytes(data))

    def _start_audio_capture(self, *, signal_active: bool = True) -> bool:
        self._cancel_audio_finalize()
        if signal_active:
            self._mic_signal_active = True
        if self._suppress_audio_until_mic_release and self._mic_signal_active:
            return False
        if time.monotonic() - self._last_silent_abort_at < WIZPR_SILENT_CAPTURE_REARM_SECONDS:
            return False
        if self.audio.active:
            return True
        self._audio_generation += 1
        self._audio_session_id += 1
        self.audio.start()
        self._schedule_audio_watchdog(self._audio_generation)
        self._schedule_audio_silence_check(self._audio_generation)
        return True

    def _schedule_audio_silence_check(self, generation: int) -> None:
        self._cancel_audio_silence_check()

        async def _run() -> None:
            try:
                await asyncio.sleep(WIZPR_SILENT_CAPTURE_ABORT_SECONDS)
                if generation != self._audio_generation or not self.audio.active or self._mic_announced:
                    return
                metrics = self.audio.activity_metrics()
                if _metrics_have_speech(metrics):
                    self._announce_mic_started()
                    return
                self.audio.discard()
                self._cancel_audio_watchdog()
                self._cancel_audio_finalize()
                self._last_silent_abort_at = time.monotonic()
                self._suppress_audio_until_mic_release = bool(self._mic_signal_active)
                await self.bus.publish(
                    "audio_ignored",
                    {
                        "reason": "silent_capture",
                        "session_id": self._audio_session_id,
                        **metrics,
                    },
                )
            except asyncio.CancelledError:
                return

        self._audio_silence_task = asyncio.create_task(_run())

    def _cancel_audio_silence_check(self) -> None:
        task = self._audio_silence_task
        self._audio_silence_task = None
        if task is not None and not task.done():
            task.cancel()

    def _announce_mic_started(self) -> None:
        if self._mic_announced or not self.audio.active:
            return
        self._mic_announced = True
        self._spawn(
            self.bus.publish(
                "mic_on",
                {
                    "uuid": self._channels.audio or WIZPR_AUDIO_CHAR_UUID,
                    "text": "AUDIO_STREAM_STARTED",
                    "session_id": self._audio_session_id,
                },
            )
        )

    def _schedule_audio_watchdog(self, generation: int) -> None:
        self._cancel_audio_watchdog()

        async def _run() -> None:
            try:
                await asyncio.sleep(WIZPR_AUDIO_MAX_CAPTURE_SECONDS)
                if generation != self._audio_generation or not self.audio.active:
                    return
                self._mic_signal_active = False
                self._schedule_audio_finalize(delay=0.0, require_mic_inactive=False, extend=True)
            except asyncio.CancelledError:
                return

        self._audio_watchdog_task = asyncio.create_task(_run())

    def _cancel_audio_watchdog(self) -> None:
        task = self._audio_watchdog_task
        self._audio_watchdog_task = None
        if task is not None and not task.done():
            task.cancel()

    def _schedule_audio_finalize(
        self,
        delay: float | None = None,
        *,
        require_mic_inactive: bool = True,
        extend: bool = False,
    ) -> None:
        task = self._audio_finalize_task
        if task is not None and not task.done() and not extend:
            return
        self._cancel_audio_finalize()
        generation = self._audio_generation
        session_id = self._audio_session_id
        finalize_delay = self.audio_finalize_delay if delay is None else max(0.0, float(delay))

        async def _run() -> None:
            try:
                if finalize_delay:
                    await asyncio.sleep(finalize_delay)
                if generation != self._audio_generation:
                    return
                if require_mic_inactive and self._mic_signal_active:
                    return
                snapshot = self.audio.detach()
                self._cancel_audio_watchdog()
                self._cancel_audio_silence_check()
                if self._mic_announced:
                    self._mic_announced = False
                    await self.bus.publish(
                        "mic_off",
                        {
                            "uuid": self._channels.mic_state or self._channels.audio,
                            "text": "AUDIO_STREAM_STOPPED",
                            "session_id": session_id,
                        },
                    )
                if snapshot is None:
                    return
                path = await asyncio.to_thread(self.audio.save_snapshot, snapshot)
                if path is not None and generation == self._audio_generation:
                    await self.bus.publish(
                        "audio_capture",
                        {
                            "path": str(path),
                            "session_id": session_id,
                            "finalize_delay_seconds": finalize_delay,
                            "captured_at": time.time(),
                        },
                    )
            except asyncio.CancelledError:
                return

        self._audio_finalize_task = asyncio.create_task(_run())

    def _cancel_audio_finalize(self) -> None:
        task = self._audio_finalize_task
        self._audio_finalize_task = None
        if task is not None and not task.done():
            task.cancel()

    def _handle_mic_state_event(self, char_uuid: str, data: bytes, text: str) -> bool:
        if not data:
            return False
        value = data[0]
        if value not in (0, 1, WIZPR_TRANSFER_START, WIZPR_TRANSFER_STOP):
            return False
        active = value in (1, WIZPR_TRANSFER_START)
        now = time.monotonic()
        normalized = 1 if active else 0
        if self._last_mic_value == normalized and now - self._last_mic_event_at < WIZPR_MIC_SIGNAL_DEBOUNCE_SECONDS:
            return True
        self._last_mic_value = normalized
        self._last_mic_event_at = now

        if active:
            was_active = self._mic_signal_active
            capture_ready = self._start_audio_capture()
            if capture_ready and not was_active:
                self._spawn(
                    self.bus.publish(
                        "mic_pre_on",
                        {
                            "uuid": char_uuid,
                            "text": text or "AUDIO_STREAM_ARMED",
                            "session_id": self._audio_session_id,
                        },
                    )
                )
            return True

        self._mic_signal_active = False
        self._suppress_audio_until_mic_release = False
        if self.audio.active and self.audio.packet_count:
            self._schedule_audio_finalize()
        elif self.audio.active:
            self.audio.discard()
            self._cancel_audio_watchdog()
            self._cancel_audio_silence_check()
        return True

    def _operation_controls_capture(self) -> bool:
        return not self._channels.mic_state

    def _handle_text_event(self, text: str, char_uuid: str) -> None:
        norm = text.strip()
        upper = norm.upper()
        self._spawn(self.bus.publish("ring_event", {"uuid": char_uuid, "text": norm}))

        if "CLICK" in upper:
            self._queue_click()
        elif "MIC_PRE_ON" in upper:
            if self._operation_controls_capture():
                was_active = self._mic_signal_active
                capture_ready = self._start_audio_capture()
                if capture_ready and not was_active:
                    self._spawn(
                        self.bus.publish(
                            "mic_pre_on",
                            {"uuid": char_uuid, "text": norm, "session_id": self._audio_session_id},
                        )
                    )
        elif "MIC_ON" in upper:
            if self._operation_controls_capture():
                self._start_audio_capture()
        elif "MIC_OFF" in upper:
            self._mic_signal_active = False
            self._suppress_audio_until_mic_release = False
            if self.audio.active and self.audio.packet_count:
                self._schedule_audio_finalize()
            elif self.audio.active:
                self.audio.discard()
                self._cancel_audio_watchdog()
                self._cancel_audio_silence_check()
        elif battery := parse_battery_update(norm):
            voltage, level = battery
            self._spawn(
                self.bus.publish(
                    "battery",
                    {"uuid": char_uuid, "text": norm, "voltage": voltage, "level": level},
                )
            )
        elif upper.startswith("VER "):
            self._spawn(self.bus.publish("version", {"uuid": char_uuid, "text": norm}))
        elif upper.startswith("PROXY") or upper.startswith("VIDLE"):
            self._spawn(self.bus.publish("proxy", {"uuid": char_uuid, "text": norm}))
        elif "POWER_OFF" in upper:
            self._spawn(self.bus.publish("power_off", {"uuid": char_uuid, "text": norm}))
        elif upper.startswith("LOCK"):
            self._spawn(self.bus.publish("lock", {"uuid": char_uuid, "text": norm}))
        elif upper.startswith("SLEEP"):
            self._spawn(self.bus.publish("sleep", {"uuid": char_uuid, "text": norm, "state": "sleep"}))
        else:
            self._publish_basic_text_event(char_uuid, norm.lower())

    def _on_audio_notify(self, _sender: int, data: bytearray) -> None:
        packet = bytes(data)
        if not packet:
            return
        if self._suppress_audio_until_mic_release and self._mic_signal_active:
            return
        if not self.audio.active and not self._start_audio_capture(signal_active=False):
            return
        if not self.audio.active:
            return
        self._last_audio_packet_at = time.monotonic()
        self.audio.feed(packet)
        if (
            not self._mic_announced
            and self.audio.packet_count >= WIZPR_RECORDING_ANNOUNCE_MIN_PACKETS
            and self.audio.has_speech_like_activity()
        ):
            self._announce_mic_started()
        if not self._channels.mic_state:
            self._schedule_audio_finalize(
                delay=self.audio_idle_finalize_delay,
                require_mic_inactive=False,
                extend=True,
            )
        elif not self._mic_signal_active:
            self._schedule_audio_finalize(
                delay=self.audio_finalize_delay,
                require_mic_inactive=False,
                extend=True,
            )

    def _on_mic_state_notify(self, _sender: int, data: bytearray) -> None:
        text = self._decode_text(data)
        char_uuid = self._channels.mic_state or WIZPR_MIC_STATE_CHAR_UUID
        self._publish_raw(char_uuid, data, text)
        self._handle_mic_state_event(char_uuid, bytes(data), text)

    def _queue_click(self) -> None:
        self._click_count += 1
        if self._click_task is not None and not self._click_task.done():
            self._click_task.cancel()
        self._click_task = asyncio.create_task(self._publish_click_after_debounce())

    async def _publish_click_after_debounce(self) -> None:
        try:
            await asyncio.sleep(WIZPR_DOUBLE_CLICK_WINDOW_SECONDS)
        except asyncio.CancelledError:
            return

        count = self._click_count
        self._click_count = 0
        await self._publish_click_count(count)

    async def _publish_click_count(self, count: int) -> None:
        payload = {"count": count, "text": "CLICK"}
        await self.bus.publish(click_topic_for_count(count), payload)
        if count == 5:
            await self.bus.publish("sos", payload)

    def _publish_basic_text_event(self, char_uuid: str, text: str) -> None:
        if text in ("single", "button_single", "tap"):
            self._spawn(self.bus.publish("button_single", {"uuid": char_uuid, "text": text}))
        elif text in ("double", "button_double", "dbl"):
            self._spawn(self.bus.publish("button_double", {"uuid": char_uuid, "text": text}))
        elif text in ("triple", "button_triple"):
            self._spawn(self.bus.publish("button_triple", {"uuid": char_uuid, "text": text}))
        elif text in ("quad", "button_quad", "quadruple"):
            self._spawn(self.bus.publish("button_quad", {"uuid": char_uuid, "text": text}))
        elif text in ("five", "button_five", "sos"):
            self._spawn(self.bus.publish("button_five", {"uuid": char_uuid, "text": text}))
            self._spawn(self.bus.publish("sos", {"uuid": char_uuid, "text": text}))
        elif text in ("long", "button_long", "hold"):
            self._spawn(self.bus.publish("button_long", {"uuid": char_uuid, "text": text}))

    @staticmethod
    def _decode_text(data: bytearray) -> str:
        return bytes(data).decode("utf-8", errors="replace").strip()

    @staticmethod
    def _looks_like_text(text: str, data: bytearray) -> bool:
        if not text or "\ufffd" in text:
            return False
        printable = sum(1 for ch in text if ch.isprintable() or ch.isspace())
        return printable / max(len(text), 1) > 0.85 and len(data) <= 96

    @staticmethod
    def _sender_uuid(sender: int) -> str:
        return str(getattr(sender, "uuid", sender))

    def _publish_raw(self, char_uuid: str, data: bytearray, text: str) -> None:
        self._spawn(
            self.bus.publish(
                "raw_notify",
                {
                    "uuid": char_uuid,
                    "data_hex": data.hex(),
                    "text": text,
                },
            )
        )
