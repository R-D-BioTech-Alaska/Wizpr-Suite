from __future__ import annotations

import json
import tempfile
import unittest
import wave
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from wizpr_suite.ble.ring_controller import (
    RingAudioCapture,
    WIZPR_AUDIO_SAMPLE_RATE,
    WIZPR_MIN_AUDIO_CAPTURE_PACKETS,
)


INDEX_TABLE = (-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8)
STEP_TABLE = (
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


class RingAudioCaptureTests(unittest.TestCase):
    def test_packet_count_tracks_buffered_audio(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            capture = RingAudioCapture(Path(td))

            self.assertEqual(0, capture.packet_count)
            capture.start()
            capture.feed(b"\x11\x22")
            capture.feed(b"\x33")

            self.assertEqual(2, capture.packet_count)

    def test_decodes_wizpr_ima_packets_high_nibble_first(self) -> None:
        packets = [bytes([0x17, 0xF0]), bytes([0x22])]

        decoded = RingAudioCapture(Path(tempfile.gettempdir()))._decode_packets(packets)

        self.assertEqual(decoded, _decode_like_wizpr_apk(b"\x17\xf0\x22"))

    def test_strips_strict_big_endian_sequence_prefix(self) -> None:
        payloads = [bytes([idx + 1]) * 8 for idx in range(10)]
        packets = [idx.to_bytes(2, "big") + payload for idx, payload in enumerate(payloads)]

        decoded = RingAudioCapture(Path(tempfile.gettempdir()))._decode_packets(packets)

        self.assertEqual(decoded, _decode_like_wizpr_apk(b"".join(payloads)))

    def test_does_not_strip_one_byte_packet_counters(self) -> None:
        payloads = [bytes([idx + 1]) * 8 for idx in range(10)]
        packets = [bytes([idx]) + payload for idx, payload in enumerate(payloads)]

        decoded = RingAudioCapture(Path(tempfile.gettempdir()))._decode_packets(packets)

        self.assertEqual(decoded, _decode_like_wizpr_apk(b"".join(packets)))

    def test_stop_and_save_writes_16khz_mono_wav(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            capture = RingAudioCapture(Path(tmp))
            capture.start()
            for _idx in range(WIZPR_MIN_AUDIO_CAPTURE_PACKETS):
                capture.feed(bytes([0x11]) * 224)

            path = capture.stop_and_save()

            self.assertIsNotNone(path)
            assert path is not None
            with wave.open(str(path), "rb") as wav:
                self.assertEqual(wav.getframerate(), WIZPR_AUDIO_SAMPLE_RATE)
                self.assertEqual(wav.getnchannels(), 1)
                self.assertEqual(wav.getsampwidth(), 2)
                self.assertEqual(wav.getnframes(), WIZPR_MIN_AUDIO_CAPTURE_PACKETS * 448)

            metadata = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(path.name, metadata["wav"])
            self.assertEqual("WIZPR Ring", metadata["source"])
            self.assertEqual(WIZPR_AUDIO_SAMPLE_RATE, metadata["sample_rate"])
            self.assertEqual(1, metadata["channels"])
            self.assertEqual(WIZPR_MIN_AUDIO_CAPTURE_PACKETS, metadata["packet_count"])
            self.assertEqual(WIZPR_MIN_AUDIO_CAPTURE_PACKETS * 224, metadata["packet_bytes"])
            self.assertEqual(WIZPR_MIN_AUDIO_CAPTURE_PACKETS * 448 * 2, metadata["pcm_bytes"])

    def test_stop_and_save_ignores_tiny_fragment_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            capture = RingAudioCapture(Path(tmp))
            capture.start()
            capture.feed(bytes([0x11]) * 224)

            self.assertIsNone(capture.stop_and_save())

    def test_stop_and_save_never_overwrites_same_timestamp_capture(self) -> None:
        class FrozenDateTime:
            @staticmethod
            def now() -> datetime:
                return datetime(2026, 7, 18, 12, 34, 56, 123000)

        with tempfile.TemporaryDirectory() as tmp:
            capture = RingAudioCapture(Path(tmp), min_packets=1, min_seconds=0.01)
            paths: list[Path] = []
            with patch("wizpr_suite.ble.ring_controller.datetime", FrozenDateTime):
                for _idx in range(2):
                    capture.start()
                    capture.feed(bytes([0x11]) * 224)
                    path = capture.stop_and_save()
                    self.assertIsNotNone(path)
                    assert path is not None
                    paths.append(path)

            self.assertNotEqual(paths[0], paths[1])
            self.assertTrue(paths[0].exists())
            self.assertTrue(paths[1].exists())

    def test_sequence_packets_drop_duplicates_without_repeating_audio(self) -> None:
        payloads = [bytes([0x11 + index]) * 12 for index in range(8)]
        packets: list[bytes] = []
        for index, payload in enumerate(payloads):
            packet = index.to_bytes(2, "big") + payload
            packets.append(packet)
            if index == 3:
                packets.append(packet)

        capture = RingAudioCapture(Path(tempfile.gettempdir()))
        decoded, stats = capture._decode_packets_with_stats(packets)

        self.assertEqual(_decode_like_wizpr_apk(b"".join(payloads)), decoded)
        self.assertEqual(2, stats["sequence_prefix_bytes"])
        self.assertEqual(1, stats["duplicate_packets"])
        self.assertEqual(8, stats["decoded_packets"])

    def test_sequence_packets_report_gaps_without_decoding_counter_bytes(self) -> None:
        packets = [
            (0).to_bytes(2, "big") + bytes([0x11]) * 12,
            (1).to_bytes(2, "big") + bytes([0x22]) * 12,
            (3).to_bytes(2, "big") + bytes([0x33]) * 12,
            (4).to_bytes(2, "big") + bytes([0x44]) * 12,
            (5).to_bytes(2, "big") + bytes([0x55]) * 12,
            (6).to_bytes(2, "big") + bytes([0x66]) * 12,
        ]

        capture = RingAudioCapture(Path(tempfile.gettempdir()))
        decoded, stats = capture._decode_packets_with_stats(packets)

        self.assertEqual(_decode_like_wizpr_apk(b"".join(packet[2:] for packet in packets)), decoded)
        self.assertEqual(1, stats["missing_packets"])


    def test_sequence_packets_are_reordered_before_decode(self) -> None:
        payloads = [bytes([0x81 + index]) * 12 for index in range(8)]
        order = [0, 1, 3, 2, 4, 5, 7, 6]
        packets = [index.to_bytes(2, "big") + payloads[index] for index in order]

        capture = RingAudioCapture(Path(tempfile.gettempdir()))
        decoded, stats = capture._decode_packets_with_stats(packets)

        self.assertEqual(_decode_like_wizpr_apk(b"".join(payloads)), decoded)
        self.assertEqual(2, stats["out_of_order_packets"])
        self.assertEqual(8, stats["decoded_packets"])


def _decode_like_wizpr_apk(data: bytes) -> bytes:
    sample = 0
    step_index = 0
    out = bytearray()
    for byte in data:
        for code in ((byte >> 4) & 0x0F, byte & 0x0F):
            step = STEP_TABLE[step_index]
            diff = step >> 3
            if code & 0x01:
                diff += step >> 2
            if code & 0x02:
                diff += step >> 1
            if code & 0x04:
                diff += step
            if code & 0x08:
                diff = -diff
            sample = max(-32768, min(32767, sample + diff))
            step_index = max(0, min(88, step_index + INDEX_TABLE[code & 0x0F]))
            out.extend(int(sample).to_bytes(2, "little", signed=True))
    return bytes(out)


if __name__ == "__main__":
    unittest.main()
