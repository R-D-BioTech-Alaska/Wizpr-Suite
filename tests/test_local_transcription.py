from __future__ import annotations

import asyncio
import math
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wizpr_suite.core.local_transcription import (
    _select_speech_region,
    _PersistentTranscriptionWorker,
    _clean_transcript,
    _reject_transcript_reason,
    _transcribe_sync,
    _transcription_worker_command,
    _usable_segment_texts,
    audio_preflight_reason,
    local_transcription_request_timeout_seconds,
    local_transcription_uses_persistent_worker,
)


def _write_wav(path: Path, samples: list[int], sample_rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))


class LocalTranscriptionTests(unittest.TestCase):
    def test_rejects_prompt_leak_variants(self) -> None:
        samples = [
            "Short spoken command to a computer assistant. Words may include Codex, OpenCode, Ollama, files, code, app, ring.",
            "Ollama Qwen clipboard paste assistant ring files code app",
        ]

        for text in samples:
            with self.subTest(text=text):
                self.assertIn("prompt hallucination", _reject_transcript_reason(text))

    def test_rejects_caption_style_descriptions(self) -> None:
        for text in (
            "Ticking sounds.",
            "This audio contains no clear speech.",
            "Sounds of keyboard clicking.",
            "Transcription: no speech detected.",
            "Subtitles by Amara.org",
        ):
            with self.subTest(text=text):
                self.assertIn("non-speech", _reject_transcript_reason(text))

    def test_rejects_boilerplate_and_filler_only_transcripts(self) -> None:
        self.assertIn("boilerplate", _reject_transcript_reason("Thank you for watching."))
        self.assertIn("filler-only", _reject_transcript_reason("Um, uh."))

    def test_rejects_silence_generated_subtitle_domain_hallucination(self) -> None:
        reason = _reject_transcript_reason("Subs by www.zeoranger.co.uk")

        self.assertIn("hallucination", reason)

    def test_keeps_real_wake_commands(self) -> None:
        self.assertEqual("", _reject_transcript_reason("Codex, open the Wizpr Suite files."))
        self.assertEqual("", _reject_transcript_reason("OpenCode, check the Ollama provider."))
        self.assertEqual("", _reject_transcript_reason("Codex, thanks for checking the files."))

    def test_clean_transcript_collapses_repeats_and_trailing_dash(self) -> None:
        self.assertEqual("Hello.", _clean_transcript("Hello Hello .."))
        self.assertEqual("Open Notepad", _clean_transcript("Open Notepad -"))

    def test_low_confidence_segments_are_dropped_before_text_filters(self) -> None:
        segments = [
            SimpleNamespace(
                text="Open Notepad.",
                avg_logprob=-0.9,
                no_speech_prob=0.96,
                compression_ratio=1.0,
                start=0.0,
                end=1.1,
            )
        ]

        texts, rejected = _usable_segment_texts(segments)

        self.assertEqual([], texts)
        self.assertEqual(1, rejected)

    def test_confident_segments_are_kept(self) -> None:
        segments = [
            SimpleNamespace(
                text="Codex, open the Wizpr Suite files.",
                avg_logprob=-0.16,
                no_speech_prob=0.04,
                compression_ratio=1.05,
                start=0.0,
                end=2.0,
            )
        ]

        texts, rejected = _usable_segment_texts(segments)

        self.assertEqual(["Codex, open the Wizpr Suite files."], texts)
        self.assertEqual(0, rejected)

    def test_clear_short_command_segments_are_kept(self) -> None:
        segments = [
            SimpleNamespace(
                text="Open Notepad.",
                avg_logprob=-0.42,
                no_speech_prob=0.44,
                compression_ratio=1.0,
                start=0.0,
                end=0.8,
            )
        ]

        texts, rejected = _usable_segment_texts(segments)

        self.assertEqual(["Open Notepad."], texts)
        self.assertEqual(0, rejected)

    def test_confident_short_command_segments_are_kept(self) -> None:
        segments = [
            SimpleNamespace(
                text="Open Notepad.",
                avg_logprob=-0.12,
                no_speech_prob=0.05,
                compression_ratio=1.0,
                start=0.0,
                end=0.8,
            )
        ]

        texts, rejected = _usable_segment_texts(segments)

        self.assertEqual(["Open Notepad."], texts)
        self.assertEqual(0, rejected)

    def test_mixed_confidence_segments_keep_only_usable_text(self) -> None:
        segments = [
            SimpleNamespace(
                text="Ticking sounds.",
                avg_logprob=-1.2,
                no_speech_prob=0.88,
                compression_ratio=1.0,
                start=0.0,
                end=0.8,
            ),
            SimpleNamespace(
                text="OpenCode, check the provider.",
                avg_logprob=-0.22,
                no_speech_prob=0.08,
                compression_ratio=1.0,
                start=0.8,
                end=2.4,
            ),
        ]

        texts, rejected = _usable_segment_texts(segments)

        self.assertEqual(["OpenCode, check the provider."], texts)
        self.assertEqual(1, rejected)

    def test_audio_preflight_rejects_silence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "silence.wav"
            _write_wav(path, [0] * 16000)

            reason, metrics = audio_preflight_reason(path)

            self.assertIn("quiet", reason)
            self.assertAlmostEqual(1.0, metrics["duration_seconds"], places=2)

    def test_audio_preflight_rejects_short_blip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "blip.wav"
            samples = [0] * 16000
            for idx in range(1200, 1280):
                samples[idx] = 12000
            _write_wav(path, samples)

            reason, _metrics = audio_preflight_reason(path, min_active_seconds=0.18)

            self.assertIn("too little", reason)

    def test_audio_preflight_rejects_scattered_click_bursts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "clicks.wav"
            samples = [0] * 32000
            for start in range(400, 31000, 1600):
                for idx in range(start, min(start + 160, len(samples))):
                    samples[idx] = 18000 if idx % 2 else -18000
            _write_wav(path, samples)

            reason, metrics = audio_preflight_reason(path, min_active_seconds=0.08)

            self.assertIn("continuous", reason)
            self.assertGreater(metrics["active_seconds"], metrics["active_run_seconds"])

    def test_audio_preflight_keeps_sustained_audio(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "voiceish.wav"
            samples = [
                int(7000 * math.sin(2.0 * math.pi * 220.0 * idx / 16000.0))
                for idx in range(16000)
            ]
            _write_wav(path, samples)

            reason, metrics = audio_preflight_reason(path)

            self.assertEqual("", reason)
            self.assertGreater(metrics["active_seconds"], 0.5)

    def test_audio_preflight_default_keeps_borderline_ring_audio_for_stt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "borderline.wav"
            samples = [0] * 16000
            for idx in range(1200, 1280):
                samples[idx] = 12000
            _write_wav(path, samples)

            reason, metrics = audio_preflight_reason(path)

            self.assertEqual("", reason)
            self.assertGreater(metrics["rms"], 0.0025)

    def test_local_whisper_uses_pretrimmed_audio_without_second_vad_pass(self) -> None:
        class FakeModel:
            def __init__(self) -> None:
                self.kwargs: dict[str, object] = {}

            def transcribe(self, audio: object, **kwargs: object):
                self.kwargs = kwargs
                segment = SimpleNamespace(
                    text="Codex, open the Wizpr Suite files.",
                    avg_logprob=-0.12,
                    no_speech_prob=0.02,
                    compression_ratio=1.0,
                    start=0.0,
                    end=1.2,
                )
                return [segment], SimpleNamespace()

        fake = FakeModel()
        with patch("wizpr_suite.core.local_transcription._get_model", return_value=fake):
            text = _transcribe_sync(Path("missing.wav"), "small.en", "int8")

        self.assertEqual("Codex, open the Wizpr Suite files.", text)
        self.assertIs(False, fake.kwargs["vad_filter"])
        self.assertEqual(2, fake.kwargs["beam_size"])
        self.assertEqual(1, fake.kwargs["best_of"])
        self.assertIn("Codex", fake.kwargs["hotwords"])
        self.assertNotIn("vad_parameters", fake.kwargs)

    def test_local_whisper_retries_unclear_short_audio_with_higher_beam(self) -> None:
        class FakeModel:
            def __init__(self) -> None:
                self.beams: list[int] = []

            def transcribe(self, audio: object, **kwargs: object):
                beam = int(kwargs["beam_size"])
                self.beams.append(beam)
                if beam == 2:
                    segment = SimpleNamespace(
                        text="hello",
                        avg_logprob=-0.85,
                        no_speech_prob=0.80,
                        compression_ratio=1.0,
                        start=0.0,
                        end=0.35,
                    )
                else:
                    segment = SimpleNamespace(
                        text="Hello, how are you?",
                        avg_logprob=-0.20,
                        no_speech_prob=0.05,
                        compression_ratio=1.0,
                        start=0.0,
                        end=0.80,
                    )
                return [segment], SimpleNamespace()

        fake = FakeModel()
        with patch("wizpr_suite.core.local_transcription._get_model", return_value=fake):
            text = _transcribe_sync(Path("missing.wav"), "small.en", "int8")

        self.assertEqual("Hello, how are you?", text)
        self.assertEqual([2, 5], fake.beams)


    def test_cancelled_persistent_request_keeps_warm_worker_and_discards_pending_result(self) -> None:
        class FakeStdin:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.data = b""

            def write(self, data: bytes) -> None:
                self.data = data

            async def drain(self) -> None:
                self.started.set()

        class FakeProcess:
            def __init__(self) -> None:
                self.stdin = FakeStdin()
                self.stdout = object()
                self.returncode = None

        async def run() -> None:
            worker = _PersistentTranscriptionWorker()
            process = FakeProcess()
            worker.process = process  # type: ignore[assignment]
            worker.model_name = "small.en"
            worker.compute_type = "int8"
            stopped = False

            async def ensure_started(_model: str, _compute: str) -> str:
                return ""

            async def stop() -> None:
                nonlocal stopped
                stopped = True

            worker._ensure_started = ensure_started  # type: ignore[method-assign]
            worker.stop = stop  # type: ignore[method-assign]
            task = asyncio.create_task(worker.transcribe(Path("voice.wav"), "small.en", "int8"))
            await process.stdin.started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(stopped)
            self.assertEqual({}, worker.pending)
            self.assertIn(b'"id": "1"', process.stdin.data)

        asyncio.run(run())

    def test_local_request_timeout_has_short_default_and_env_override(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(12.0, local_transcription_request_timeout_seconds())

        with patch.dict("os.environ", {"WIZPR_LOCAL_WHISPER_REQUEST_TIMEOUT": "12"}, clear=True):
            self.assertEqual(12.0, local_transcription_request_timeout_seconds())

    def test_local_transcription_defaults_to_warm_worker(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertTrue(local_transcription_uses_persistent_worker())

    def test_source_worker_command_uses_python_module(self) -> None:
        with patch.object(sys, "executable", "python.exe"):
            command = _transcription_worker_command("--server")

        self.assertEqual(
            ["python.exe", "-m", "wizpr_suite.tools.local_transcribe_worker", "--server"],
            command,
        )

    def test_frozen_worker_command_relaunches_executable(self) -> None:
        with patch.object(sys, "executable", "WizprSuite.exe"):
            with patch.object(sys, "frozen", True, create=True):
                command = _transcription_worker_command("--server", "--model", "small.en")

        self.assertEqual(
            ["WizprSuite.exe", "--local-transcribe-worker", "--server", "--model", "small.en"],
            command,
        )

    def test_preprocessing_preserves_separate_speech_phrases_in_one_turn(self) -> None:
        import numpy as np

        sample_rate = 16000
        first = np.full(int(sample_rate * 0.40), 0.08, dtype=np.float32)
        pause = np.zeros(int(sample_rate * 1.00), dtype=np.float32)
        second = np.full(int(sample_rate * 0.45), -0.09, dtype=np.float32)
        audio = np.concatenate((first, pause, second))

        selected = _select_speech_region(audio, sample_rate)

        self.assertGreater(float(np.max(selected)), 0.07)
        self.assertLess(float(np.min(selected)), -0.08)
        self.assertLess(selected.size, audio.size)


if __name__ == "__main__":
    unittest.main()
