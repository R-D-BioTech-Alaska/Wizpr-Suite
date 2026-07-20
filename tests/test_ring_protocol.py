from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wizpr_suite.ble.ble_manager import DiscoveredDevice, WIZPR_RING_SERVICE_UUID
from wizpr_suite.ble.ring_controller import (
    WIZPR_AUDIO_FINALIZE_DELAY_SECONDS,
    WIZPR_AUDIO_CHAR_UUID,
    WIZPR_DOUBLE_CLICK_WINDOW_SECONDS,
    WIZPR_BATTERY_COMMAND,
    WIZPR_COMMAND_CHAR_UUID,
    WIZPR_SERVICE_UUID,
    WIZPR_SAMPLE_RATE_COMMAND,
    WIZPR_TRANSFER_STATUS_CHAR_UUID,
    RingController,
    RingProfile,
    WizprChannels,
    battery_voltage_to_percent,
    click_topic_for_count,
    parse_battery_update,
)
from wizpr_suite.core.config import DEFAULT_MAPPINGS
from wizpr_suite.core.event_bus import EventBus


class RingProtocolTests(unittest.TestCase):
    def test_ring_name_matches_but_case_does_not(self) -> None:
        ring = DiscoveredDevice(address="AA:BB:CC:DD:EE:01", name="WIZPR RING-AA:BB", rssi=-50)
        case = DiscoveredDevice(address="AA:BB:CC:DD:EE:02", name="WIZPR CASE-AA:BB", rssi=-50)

        self.assertEqual(ring.candidate_label, "WIZPR ring")
        self.assertEqual(case.candidate_label, "WIZPR case")

    def test_ring_service_matches_even_without_name(self) -> None:
        ring = DiscoveredDevice(
            address="AA:BB:CC:DD:EE:03",
            name="",
            rssi=-70,
            service_uuids=[WIZPR_RING_SERVICE_UUID],
        )

        self.assertEqual(ring.candidate_label, "WIZPR ring")

    def test_battery_update_accepts_sdk_formats(self) -> None:
        self.assertEqual(parse_battery_update("BATT=3.350 OK"), (3.35, 25))
        self.assertEqual(parse_battery_update("BATTERY 75(3.524322)"), (3.524322, 55))

    def test_battery_percent_uses_spokenword_curve(self) -> None:
        self.assertEqual(battery_voltage_to_percent(3.74), 100)
        self.assertEqual(battery_voltage_to_percent(3.0), 4)
        self.assertEqual(battery_voltage_to_percent(2.5), 0)

    def test_sdk_timing_defaults(self) -> None:
        self.assertEqual(WIZPR_AUDIO_FINALIZE_DELAY_SECONDS, 0.18)
        self.assertEqual(WIZPR_DOUBLE_CLICK_WINDOW_SECONDS, 1.0)

    def test_basic_settings_follow_sdk_command_order(self) -> None:
        class CommandOnlyRing(RingController):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.commands: list[str] = []

            async def write_command(self, command: str, response: bool = True) -> None:
                self.commands.append(command)

        async def run() -> list[str]:
            ring = CommandOnlyRing(object(), EventBus(), RingProfile())
            await ring.configure_basic_settings()
            return ring.commands

        self.assertEqual(asyncio.run(run()), [WIZPR_SAMPLE_RATE_COMMAND, WIZPR_BATTERY_COMMAND])

    def test_start_session_follows_sdk_order(self) -> None:
        class SessionRing(RingController):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.calls: list[str] = []

            def has_wizpr_signature(self) -> bool:
                self.calls.append("signature")
                return True

            async def subscribe_wizpr_channels(self, strict: bool = True, include_fallback: bool = False) -> None:
                self.calls.append("subscribe")

            async def configure_basic_settings(self) -> None:
                self.calls.append("configure")

        async def run() -> list[str]:
            ring = SessionRing(object(), EventBus(), RingProfile())
            await ring.start_wizpr_session()
            return ring.calls

        self.assertEqual(asyncio.run(run()), ["signature", "subscribe", "configure"])

    def test_sdk_subscribe_uses_only_official_characteristics_by_default(self) -> None:
        async def run() -> list[str]:
            client = _FakeBleClient(
                [
                    _FakeService(
                        WIZPR_SERVICE_UUID,
                        [
                            _FakeChar(WIZPR_AUDIO_CHAR_UUID, ["notify"]),
                            _FakeChar(WIZPR_TRANSFER_STATUS_CHAR_UUID, ["notify"]),
                            _FakeChar(WIZPR_COMMAND_CHAR_UUID, ["write", "notify"]),
                            _FakeChar("00000009-dc2e-4362-93d3-df429eb3ad10", ["notify"]),
                        ],
                    )
                ]
            )
            ring = RingController(_FakeBle(client), EventBus(), RingProfile())
            await ring.subscribe_wizpr_channels()
            return client.subscribed

        self.assertEqual(
            asyncio.run(run()),
            [WIZPR_COMMAND_CHAR_UUID, WIZPR_AUDIO_CHAR_UUID, WIZPR_TRANSFER_STATUS_CHAR_UUID],
        )

    def test_basic_session_requires_official_sdk_characteristics(self) -> None:
        async def run() -> None:
            client = _FakeBleClient(
                [
                    _FakeService(
                        WIZPR_SERVICE_UUID,
                        [_FakeChar(WIZPR_COMMAND_CHAR_UUID, ["write", "notify"])],
                    )
                ]
            )
            ring = RingController(_FakeBle(client), EventBus(), RingProfile())
            await ring.start_wizpr_session()

        with self.assertRaisesRegex(RuntimeError, "official SDK characteristic"):
            asyncio.run(run())

    def test_operation_mic_events_do_not_drive_recording_when_transfer_status_exists(self) -> None:
        async def run() -> bool:
            ring = RingController(object(), EventBus(), RingProfile())
            ring._channels = WizprChannels(mic_state=WIZPR_TRANSFER_STATUS_CHAR_UUID)
            ring._handle_text_event("MIC_ON", WIZPR_COMMAND_CHAR_UUID)
            await asyncio.sleep(0)
            return ring.audio.active

        self.assertFalse(asyncio.run(run()))

    def test_transfer_status_drives_recording_boundaries(self) -> None:
        async def run() -> tuple[bool, bool]:
            ring = RingController(object(), EventBus(), RingProfile(), audio_finalize_delay=0)
            ring._channels = WizprChannels(mic_state=WIZPR_TRANSFER_STATUS_CHAR_UUID)
            ring._handle_mic_state_event(WIZPR_TRANSFER_STATUS_CHAR_UUID, b"1", "")
            started = ring.audio.active
            ring._handle_mic_state_event(WIZPR_TRANSFER_STATUS_CHAR_UUID, b"0", "")
            await asyncio.sleep(0)
            return started, ring.audio.active

        self.assertEqual(asyncio.run(run()), (True, False))

    def test_operation_mic_off_finalizes_if_transfer_stop_is_missed(self) -> None:
        async def run() -> tuple[bool, bool]:
            ring = RingController(object(), EventBus(), RingProfile(), audio_finalize_delay=0)
            ring._channels = WizprChannels(mic_state=WIZPR_TRANSFER_STATUS_CHAR_UUID)
            ring._handle_mic_state_event(WIZPR_TRANSFER_STATUS_CHAR_UUID, b"1", "")
            started = ring.audio.active
            ring._handle_text_event("MIC_OFF", WIZPR_COMMAND_CHAR_UUID)
            await asyncio.sleep(0)
            return started, ring.audio.active

        self.assertEqual(asyncio.run(run()), (True, False))

    def test_operation_mic_events_still_work_as_old_firmware_fallback(self) -> None:
        async def run() -> bool:
            ring = RingController(object(), EventBus(), RingProfile())
            ring._handle_text_event("MIC_ON", WIZPR_COMMAND_CHAR_UUID)
            await asyncio.sleep(0)
            return ring.audio.active

        self.assertTrue(asyncio.run(run()))

    def test_operation_mic_events_without_audio_do_not_publish_recording_status(self) -> None:
        async def run() -> tuple[bool, bool, list[tuple[str, dict]]]:
            bus = EventBus()
            events: list[tuple[str, dict]] = []

            async def record(topic: str):
                async def _inner(payload: dict) -> None:
                    events.append((topic, payload))

                return _inner

            for topic in ("mic_on", "mic_off"):
                await bus.subscribe(topic, await record(topic))

            ring = RingController(object(), bus, RingProfile(), audio_finalize_delay=0)
            ring._handle_text_event("EVT:MIC_ON:1", WIZPR_COMMAND_CHAR_UUID)
            started = ring.audio.active
            ring._handle_text_event("EVT:MIC_OFF:0", WIZPR_COMMAND_CHAR_UUID)
            await asyncio.sleep(0)
            return started, ring.audio.active, events

        started, active, events = asyncio.run(run())

        self.assertTrue(started)
        self.assertFalse(active)
        self.assertEqual([], [topic for topic, _payload in events])

    def test_transfer_status_only_announces_recording_after_audio_arrives(self) -> None:
        async def run() -> list[str]:
            bus = EventBus()
            events: list[str] = []

            async def record(topic: str):
                async def _inner(_payload: dict) -> None:
                    events.append(topic)

                return _inner

            await bus.subscribe("mic_on", await record("mic_on"))
            await bus.subscribe("mic_off", await record("mic_off"))

            ring = RingController(object(), bus, RingProfile(), audio_finalize_delay=0)
            ring._channels = WizprChannels(
                audio=WIZPR_AUDIO_CHAR_UUID,
                mic_state=WIZPR_TRANSFER_STATUS_CHAR_UUID,
            )
            ring._handle_mic_state_event(WIZPR_TRANSFER_STATUS_CHAR_UUID, b"1", "")
            await asyncio.sleep(0)
            self.assertEqual([], events)

            ring._on_audio_notify(0, bytearray([0x12] * 244))
            await asyncio.sleep(0)
            self.assertEqual([], events)
            for _idx in range(7):
                ring._on_audio_notify(0, bytearray([0x12] * 244))
            await asyncio.sleep(0)
            self.assertEqual(["mic_on"], events)

            ring._handle_mic_state_event(WIZPR_TRANSFER_STATUS_CHAR_UUID, b"0", "")
            await asyncio.sleep(0)
            return events

        self.assertEqual(["mic_on", "mic_off"], asyncio.run(run()))

    def test_transfer_status_prevents_idle_packet_gaps_from_fragmenting_capture(self) -> None:
        async def run() -> tuple[bool, int]:
            bus = EventBus()
            captures: list[dict] = []

            async def record(payload: dict) -> None:
                captures.append(payload)

            await bus.subscribe("audio_capture", record)
            ring = RingController(
                object(),
                bus,
                RingProfile(),
                audio_finalize_delay=0,
                audio_idle_finalize_delay=0,
            )
            ring._channels = WizprChannels(
                audio=WIZPR_AUDIO_CHAR_UUID,
                mic_state=WIZPR_TRANSFER_STATUS_CHAR_UUID,
            )
            ring._handle_mic_state_event(WIZPR_TRANSFER_STATUS_CHAR_UUID, b"1", "")
            for _idx in range(8):
                ring._on_audio_notify(0, bytearray([0x12] * 244))
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.05)
            active_during_gap = ring.audio.active
            ring._handle_mic_state_event(WIZPR_TRANSFER_STATUS_CHAR_UUID, b"0", "")
            if ring._audio_finalize_task is not None:
                await ring._audio_finalize_task
            return active_during_gap, len(captures)

        self.assertEqual((True, 1), asyncio.run(run()))

    def test_bouncing_transfer_signals_do_not_create_multiple_voice_sessions(self) -> None:
        async def run() -> tuple[int, int, int]:
            bus = EventBus()
            mic_on: list[dict] = []
            captures: list[dict] = []

            await bus.subscribe("mic_on", lambda payload: mic_on.append(payload))
            await bus.subscribe("audio_capture", lambda payload: captures.append(payload))
            with tempfile.TemporaryDirectory() as td:
                ring = RingController(
                    object(),
                    bus,
                    RingProfile(),
                    capture_dir=Path(td),
                    audio_finalize_delay=0,
                )
                ring._channels = WizprChannels(
                    audio=WIZPR_AUDIO_CHAR_UUID,
                    mic_state=WIZPR_TRANSFER_STATUS_CHAR_UUID,
                )
                for _idx in range(4):
                    ring._handle_mic_state_event(WIZPR_TRANSFER_STATUS_CHAR_UUID, b"1", "")
                for _idx in range(8):
                    ring._on_audio_notify(0, bytearray([0x92] * 244))
                for _idx in range(4):
                    ring._handle_mic_state_event(WIZPR_TRANSFER_STATUS_CHAR_UUID, b"0", "")
                if ring._audio_finalize_task is not None:
                    await ring._audio_finalize_task
                return ring._audio_session_id, len(mic_on), len(captures)

        self.assertEqual((1, 1, 1), asyncio.run(run()))

    def test_audio_packets_start_and_save_capture_if_transfer_start_is_missed(self) -> None:
        async def run() -> list[tuple[str, dict]]:
            with tempfile.TemporaryDirectory() as td:
                bus = EventBus()
                events: list[tuple[str, dict]] = []

                async def record(topic: str):
                    async def _inner(payload: dict) -> None:
                        events.append((topic, payload))

                    return _inner

                await bus.subscribe("mic_on", await record("mic_on"))
                await bus.subscribe("audio_capture", await record("audio_capture"))

                ring = RingController(
                    object(),
                    bus,
                    RingProfile(),
                    capture_dir=Path(td),
                    audio_finalize_delay=0,
                    audio_idle_finalize_delay=0,
                )
                ring._channels = WizprChannels(
                    audio=WIZPR_AUDIO_CHAR_UUID,
                    mic_state=WIZPR_TRANSFER_STATUS_CHAR_UUID,
                )
                packet = bytearray([0x12] * 244)
                for _idx in range(8):
                    ring._on_audio_notify(0, packet)
                if ring._audio_finalize_task is not None:
                    await ring._audio_finalize_task

                saved = [payload for topic, payload in events if topic == "audio_capture"]
                self.assertEqual(1, len(saved))
                self.assertTrue(Path(saved[0]["path"]).exists())
                return events

        topics = [topic for topic, _payload in asyncio.run(run())]

        self.assertIn("mic_on", topics)
        self.assertIn("audio_capture", topics)

    def test_silent_capture_is_aborted_without_recording_events(self) -> None:
        async def run() -> tuple[bool, list[str], int]:
            bus = EventBus()
            events: list[str] = []

            async def record(topic: str):
                async def _inner(_payload: dict) -> None:
                    events.append(topic)

                return _inner

            await bus.subscribe("mic_on", await record("mic_on"))
            await bus.subscribe("audio_capture", await record("audio_capture"))
            await bus.subscribe("audio_ignored", await record("audio_ignored"))
            ring = RingController(object(), bus, RingProfile(), audio_finalize_delay=0)
            ring._channels = WizprChannels(
                audio=WIZPR_AUDIO_CHAR_UUID,
                mic_state=WIZPR_TRANSFER_STATUS_CHAR_UUID,
            )
            with patch("wizpr_suite.ble.ring_controller.WIZPR_SILENT_CAPTURE_ABORT_SECONDS", 0.01):
                ring._handle_mic_state_event(WIZPR_TRANSFER_STATUS_CHAR_UUID, b"1", "")
                for _idx in range(12):
                    ring._on_audio_notify(0, bytearray([0x00] * 244))
                await asyncio.sleep(0.03)
            return ring.audio.active, events, ring._audio_session_id

        active, events, session_id = asyncio.run(run())

        self.assertFalse(active)
        self.assertEqual(["audio_ignored"], events)
        self.assertEqual(1, session_id)

    def test_click_counts_have_named_topics(self) -> None:
        self.assertEqual(click_topic_for_count(1), "button_single")
        self.assertEqual(click_topic_for_count(2), "button_double")
        self.assertEqual(click_topic_for_count(3), "button_triple")
        self.assertEqual(click_topic_for_count(4), "button_quad")
        self.assertEqual(click_topic_for_count(5), "button_five")
        self.assertEqual(click_topic_for_count(6), "button_multi")

    def test_five_clicks_emit_sos_without_default_action(self) -> None:
        self.assertFalse(any("sos" in triggers for triggers in DEFAULT_MAPPINGS.values()))

        async def run() -> list[tuple[str, dict]]:
            bus = EventBus()
            events: list[tuple[str, dict]] = []

            async def record(topic: str):
                async def _inner(payload: dict) -> None:
                    events.append((topic, payload))

                return _inner

            for topic in ("button_five", "sos"):
                await bus.subscribe(topic, await record(topic))

            ring = RingController(object(), bus, RingProfile())
            await ring._publish_click_count(5)
            return events

        events = asyncio.run(run())

        self.assertEqual(
            events,
            [
                ("button_five", {"count": 5, "text": "CLICK"}),
                ("sos", {"count": 5, "text": "CLICK"}),
            ],
        )

    def test_click_operation_matches_sdk_contains_behavior(self) -> None:
        async def run() -> list[tuple[str, dict]]:
            bus = EventBus()
            events: list[tuple[str, dict]] = []

            async def record(payload: dict) -> None:
                events.append(("button_single", payload))

            await bus.subscribe("button_single", record)
            ring = RingController(object(), bus, RingProfile())
            ring._handle_text_event("EVENT CLICK 1", WIZPR_COMMAND_CHAR_UUID)
            await asyncio.sleep(WIZPR_DOUBLE_CLICK_WINDOW_SECONDS + 0.05)
            return events

        events = asyncio.run(run())

        self.assertEqual([("button_single", {"count": 1, "text": "CLICK"})], events)

    def test_sleep_lock_and_power_off_are_named_events(self) -> None:
        async def run() -> list[tuple[str, dict]]:
            bus = EventBus()
            events: list[tuple[str, dict]] = []

            async def record(topic: str):
                async def _inner(payload: dict) -> None:
                    events.append((topic, payload))

                return _inner

            for topic in ("sleep", "lock", "power_off"):
                await bus.subscribe(topic, await record(topic))

            ring = RingController(object(), bus, RingProfile())
            ring._handle_text_event("SLEEP", "operation")
            ring._handle_text_event("LOCK", "operation")
            ring._handle_text_event("POWER_OFF", "operation")
            await asyncio.sleep(0)
            return events

        topics = [topic for topic, _payload in asyncio.run(run())]

        self.assertEqual(["sleep", "lock", "power_off"], topics)

    def test_sleep_command_updates_local_sleep_state(self) -> None:
        class CommandOnlyRing(RingController):
            async def write_command(self, command: str, response: bool = True) -> None:
                await self.bus.publish("ring_command", {"command": command})

        async def run() -> list[tuple[str, dict]]:
            bus = EventBus()
            events: list[tuple[str, dict]] = []

            async def record(topic: str):
                async def _inner(payload: dict) -> None:
                    events.append((topic, payload))

                return _inner

            await bus.subscribe("ring_command", await record("ring_command"))
            await bus.subscribe("sleep", await record("sleep"))
            ring = CommandOnlyRing(object(), bus, RingProfile())
            await ring.sleep()
            return events

        events = asyncio.run(run())

        self.assertEqual(("ring_command", {"command": "SLEEP"}), events[0])
        self.assertEqual(("sleep", {"text": "SLEEP", "source": "command"}), events[1])

class _FakeChar:
    def __init__(self, uuid: str, properties: list[str]) -> None:
        self.uuid = uuid
        self.properties = properties


class _FakeService:
    def __init__(self, uuid: str, characteristics: list[_FakeChar]) -> None:
        self.uuid = uuid
        self.characteristics = characteristics


class _FakeBleClient:
    def __init__(self, services: list[_FakeService]) -> None:
        self.services = services
        self.is_connected = True
        self.subscribed: list[str] = []

    async def start_notify(self, char_uuid: str, _callback) -> None:
        self.subscribed.append(char_uuid)


class _FakeBle:
    def __init__(self, client: _FakeBleClient) -> None:
        self.client = client


if __name__ == "__main__":
    unittest.main()
