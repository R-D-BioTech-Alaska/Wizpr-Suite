from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from wizpr_suite.ui.main_window import MainWindow


class _Prompt:
    def __init__(self, text: str = "") -> None:
        self.text = text

    def clear(self) -> None:
        self.text = ""


class _Router:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.finished: list[str] = []

    async def dispatch(self, action: str, payload: dict[str, object]) -> None:
        self.started.append(action)
        try:
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise
        self.finished.append(action)


class VoicePipelineTests(unittest.TestCase):
    def test_starting_new_capture_cancels_speech_response_and_old_pipeline(self) -> None:
        async def run() -> None:
            window = MainWindow.__new__(MainWindow)
            window._voice_capture_active = False
            window._voice_turn = 3
            window._voice_session_id = None
            window._response_generation = 0
            window._speech_generation = 0
            window._voice_capture_started_at = 0.0
            window._speech_queue = [(0, "old response")]
            window._speech_task = asyncio.create_task(asyncio.sleep(30))
            window._voice_pipeline_task = asyncio.create_task(asyncio.sleep(30))
            old_assistant = asyncio.create_task(asyncio.sleep(30))
            window._assistant_tasks = {old_assistant}
            window.prompt = _Prompt("old transcript")
            window._update_talk_action_state = lambda: None
            window._set_voice_status = lambda _text: None

            speech = window._speech_task
            pipeline = window._voice_pipeline_task
            window._begin_voice_capture()
            await asyncio.sleep(0)

            self.assertEqual(4, window._voice_turn)
            self.assertEqual("", window.prompt.text)
            self.assertTrue(speech.cancelled())
            self.assertTrue(pipeline.cancelled())
            self.assertTrue(old_assistant.cancelled())
            self.assertEqual([], window._speech_queue)

        asyncio.run(run())

    def test_latest_capture_replaces_previous_pipeline(self) -> None:
        async def run() -> None:
            window = MainWindow.__new__(MainWindow)
            window.loop = asyncio.get_running_loop()
            window.router = _Router()
            window._voice_turn = 0
            window._voice_session_id = None
            window._response_generation = 0
            window._speech_generation = 0
            window._voice_capture_active = False
            window._voice_pipeline_task = None

            window._schedule_voice_capture_action("first", {"payload": {"path": "first.wav"}})
            first = window._voice_pipeline_task
            await asyncio.sleep(0)
            window._schedule_voice_capture_action("second", {"payload": {"path": "second.wav"}})
            second = window._voice_pipeline_task
            await asyncio.sleep(0.08)

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertTrue(first.done())
            self.assertEqual(["first", "second"], window.router.started)
            self.assertEqual(["second"], window.router.finished)

        asyncio.run(run())


    def test_phrase_interrupt_mode_does_not_cancel_response_on_ring_movement(self) -> None:
        async def run() -> None:
            window = MainWindow.__new__(MainWindow)
            window.cfg = SimpleNamespace(
                ring_voice_target="assistant",
                transcription=SimpleNamespace(interrupt_mode="word", interrupt_word="stop"),
            )
            window._voice_capture_active = False
            window._voice_turn = 4
            window._voice_session_id = None
            window._response_generation = 2
            window._speech_generation = 0
            window._voice_capture_started_at = 0.0
            window._speech_queue = [(0, "current response")]
            window._speech_task = asyncio.create_task(asyncio.sleep(30))
            window._voice_pipeline_task = None
            window._voice_interrupt_probe_task = None
            window._assistant_tasks = set()
            window.prompt = _Prompt("keep this")
            window._update_talk_action_state = lambda: None
            statuses: list[str] = []
            window._set_voice_status = statuses.append

            speech = window._speech_task
            window._begin_voice_capture()
            await asyncio.sleep(0)

            self.assertEqual(4, window._voice_turn)
            self.assertEqual("keep this", window.prompt.text)
            self.assertFalse(speech.cancelled())
            self.assertTrue(window._capture_waiting_for_interrupt_word)
            self.assertIn("interrupt phrase", statuses[-1])
            speech.cancel()

        asyncio.run(run())

    def test_interrupt_phrase_is_removed_from_followup_command(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window.cfg = SimpleNamespace(
            transcription=SimpleNamespace(interrupt_word="stop")
        )

        matched, command = window._interrupt_command_from_transcript(
            "Please pause. stop, open the calculator"
        )

        self.assertTrue(matched)
        self.assertEqual("Please pause. open the calculator", command)

    def test_interrupt_probe_does_not_cancel_active_voice_response(self) -> None:
        async def run() -> None:
            window = MainWindow.__new__(MainWindow)
            window.loop = asyncio.get_running_loop()
            window.cfg = SimpleNamespace(
                ring_voice_target="assistant",
                transcription=SimpleNamespace(interrupt_mode="word"),
            )
            window.router = _Router()
            window._voice_turn = 2
            window._voice_capture_generation = 0
            window._voice_session_id = None
            window._response_generation = 1
            window._active_response_count = 1
            window._speech_generation = 0
            window._voice_capture_active = False
            window._voice_pipeline_task = asyncio.create_task(asyncio.sleep(30))
            window._voice_interrupt_probe_task = None
            window._assistant_tasks = set()
            window._speech_task = None
            window._speech_queue = []
            window._set_voice_status = lambda _text: None

            response = window._voice_pipeline_task
            window._schedule_voice_capture_action(
                "send_audio_to_assistant", {"payload": {"path": "interrupt.wav"}}
            )
            probe = window._voice_interrupt_probe_task
            await asyncio.sleep(0.01)

            self.assertFalse(response.cancelled())
            self.assertIsNotNone(probe)
            self.assertIs(window._voice_pipeline_task, response)
            response.cancel()
            if probe is not None:
                probe.cancel()

        asyncio.run(run())

    def test_spoken_stream_chunks_on_complete_sentences(self) -> None:
        first, remaining = MainWindow._next_spoken_chunk("This is ready. The next sentence is still coming")
        self.assertEqual("This is ready.", first)
        self.assertEqual("The next sentence is still coming", remaining)

        final, remaining = MainWindow._next_spoken_chunk(remaining, force=True)
        self.assertEqual("The next sentence is still coming", final)
        self.assertEqual("", remaining)

    def test_stale_voice_turn_stops_stream_before_new_chunks_or_speech(self) -> None:
        class _Provider:
            def __init__(self, window: MainWindow) -> None:
                self.window = window

            async def stream_generate(self, prompt: str, model: str, temperature: float):
                yield "Old response is still "
                self.window._voice_turn += 1
                yield "speaking after interruption."

        async def run() -> None:
            window = MainWindow.__new__(MainWindow)
            window.loop = asyncio.get_running_loop()
            window._voice_turn = 8
            window._response_generation = 2
            output: list[str] = []
            spoken: list[str] = []
            window._append_output_text = output.append
            window._maybe_speak_response = lambda text, remember=False: spoken.append(text)

            result = await window._stream_provider_response(
                _Provider(window),
                "hello",
                "model",
                0.2,
                speak_sentences=True,
                voice_turn=8,
                response_generation=2,
            )

            self.assertEqual("Old response is still ", result)
            self.assertEqual([], output)
            self.assertEqual([], spoken)

        asyncio.run(run())

    def test_stop_is_a_standalone_hard_interrupt_and_not_part_of_desktop(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window.cfg = SimpleNamespace(transcription=SimpleNamespace(interrupt_word="stop"))

        matched, remainder = window._interrupt_command_from_transcript("Stop.")
        self.assertTrue(matched)
        self.assertEqual("", remainder)

        matched, remainder = window._interrupt_command_from_transcript("open desktop")
        self.assertFalse(matched)
        self.assertEqual("open desktop", remainder)

    def test_typed_stop_cancels_without_sending_a_new_prompt(self) -> None:
        async def run() -> None:
            class Prompt:
                def __init__(self) -> None:
                    self.value = "stop"

                def toPlainText(self) -> str:
                    return self.value

                def clear(self) -> None:
                    self.value = ""

            window = MainWindow.__new__(MainWindow)
            window.loop = asyncio.get_running_loop()
            window.cfg = SimpleNamespace(transcription=SimpleNamespace(interrupt_word="stop"))
            window.prompt = Prompt()
            window._voice_turn = 2
            window._response_generation = 4
            window._speech_generation = 0
            window._active_response_count = 1
            window._speech_queue = [(0, "still speaking")]
            window._speech_task = asyncio.create_task(asyncio.sleep(30))
            response = asyncio.create_task(asyncio.sleep(30))
            window._assistant_tasks = {response}
            window._voice_pipeline_task = None
            window._update_talk_action_state = lambda: None
            window._set_voice_status = lambda _text: None
            window.statusBar = lambda: SimpleNamespace(showMessage=lambda *_args: None)

            speech = window._speech_task
            window._send_chat()
            await asyncio.sleep(0)

            self.assertEqual("", window.prompt.value)
            self.assertTrue(speech.cancelled())
            self.assertTrue(response.cancelled())
            self.assertEqual([], window._speech_queue)
            self.assertEqual(3, window._voice_turn)
            self.assertEqual(5, window._response_generation)

        asyncio.run(run())


    def test_typed_stop_is_normal_input_while_idle(self) -> None:
        async def run() -> None:
            class Prompt:
                def __init__(self) -> None:
                    self.value = "stop"

                def toPlainText(self) -> str:
                    return self.value

                def clear(self) -> None:
                    self.value = ""

            sent: list[str] = []
            window = MainWindow.__new__(MainWindow)
            window.loop = asyncio.get_running_loop()
            window.cfg = SimpleNamespace(transcription=SimpleNamespace(interrupt_word="stop"))
            window.prompt = Prompt()
            window._voice_turn = 2
            window._response_generation = 4
            window._active_response_count = 0
            window._speech_generation = 0
            window._speech_queue = []
            window._speech_task = None
            window._assistant_tasks = set()
            window._voice_pipeline_task = None
            window._update_talk_action_state = lambda: None
            window._set_voice_status = lambda _text: None
            window.statusBar = lambda: SimpleNamespace(showMessage=lambda *_args: None)

            async def handle_tool(_prompt: str) -> bool:
                return False

            async def send_prompt(prompt: str, voice_turn=None) -> None:
                sent.append(prompt)

            window._handle_desktop_tool = handle_tool
            window._send_prompt_to_assistant = send_prompt
            window._send_chat()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            self.assertEqual(["stop"], sent)
            self.assertEqual("", window.prompt.value)
            self.assertEqual(5, window._response_generation)
            self.assertEqual(2, window._voice_turn)

        asyncio.run(run())

    def test_response_activity_ignores_transcription_only_pipeline(self) -> None:
        async def run() -> None:
            window = MainWindow.__new__(MainWindow)
            window._active_response_count = 0
            window._speech_task = None
            window._speech_queue = []
            window._voice_pipeline_task = asyncio.create_task(asyncio.sleep(30))
            try:
                self.assertFalse(window._assistant_response_active())
                window._active_response_count = 1
                self.assertTrue(window._assistant_response_active())
            finally:
                window._voice_pipeline_task.cancel()

        asyncio.run(run())

    def test_spoken_stop_is_normal_request_without_response_gate(self) -> None:
        async def run() -> None:
            import tempfile
            from pathlib import Path

            sent: list[str] = []

            class Prompt:
                def clear(self) -> None:
                    return None

            window = MainWindow.__new__(MainWindow)
            window.cfg = SimpleNamespace(transcription=SimpleNamespace(interrupt_word="stop"))
            window.prompt = Prompt()
            window._voice_turn = 7
            window._voice_capture_generation = 5
            window._last_transcript = ""
            window._transcribe_audio_file = lambda _path: asyncio.sleep(0, result="Stop.")
            window._voice_capture_is_current = lambda _generation: True
            window._voice_turn_is_current = lambda _turn: True
            window._voice_command_for_target = lambda transcript, _target: transcript
            window._auto_voice_command_ready = lambda *_args: True
            window._is_duplicate_auto_voice_command = lambda *_args: False
            window._remember_auto_voice_command = lambda *_args: None
            window._desktop_voice_command_needs_review = lambda _command: False
            window._update_talk_action_state = lambda: None
            window._append_ble_log = lambda _text: None
            window.statusBar = lambda: SimpleNamespace(showMessage=lambda *_args: None)
            window._commit_interrupt = lambda: (_ for _ in ()).throw(AssertionError("idle stop must not interrupt"))

            async def handle_tool(_command: str) -> bool:
                return False

            async def send_prompt(prompt: str, voice_turn=None) -> None:
                sent.append(prompt)

            window._handle_desktop_tool = handle_tool
            window._send_prompt_to_assistant = send_prompt

            with tempfile.TemporaryDirectory() as td:
                audio = Path(td) / "stop.wav"
                audio.write_bytes(b"stop")
                await window._send_audio_capture_to_assistant(
                    {
                        "payload": {"path": str(audio)},
                        "_voice_turn": 7,
                        "_voice_capture_generation": 5,
                        "_interrupt_gate": "",
                    }
                )

            self.assertEqual(["Stop."], sent)
            self.assertEqual("Stop.", window._last_transcript)

        asyncio.run(run())


    def test_spoken_stop_cancels_active_response_and_speech(self) -> None:
        async def run() -> None:
            import tempfile
            from pathlib import Path

            window = MainWindow.__new__(MainWindow)
            window.cfg = SimpleNamespace(transcription=SimpleNamespace(interrupt_word="stop"))
            window._voice_turn = 7
            window._response_generation = 3
            window._voice_capture_generation = 5
            window._speech_generation = 0
            window._speech_queue = [(0, "current spoken response")]
            window._speech_task = asyncio.create_task(asyncio.sleep(30))
            active_response = asyncio.create_task(asyncio.sleep(30))
            window._voice_pipeline_task = active_response
            window._assistant_tasks = set()
            window._set_voice_status = lambda _text: None
            window._append_ble_log = lambda _text: None
            window.statusBar = lambda: SimpleNamespace(showMessage=lambda *_args: None)
            window._transcribe_audio_file = lambda _path: asyncio.sleep(0, result="Stop.")

            with tempfile.TemporaryDirectory() as td:
                audio = Path(td) / "stop.wav"
                audio.write_bytes(b"stop")
                speech = window._speech_task
                await window._send_audio_capture_to_assistant(
                    {
                        "payload": {"path": str(audio)},
                        "_voice_turn": 7,
                        "_voice_capture_generation": 5,
                        "_interrupt_gate": "word",
                    }
                )
                await asyncio.sleep(0)

            self.assertTrue(speech.cancelled())
            self.assertTrue(active_response.cancelled())
            self.assertEqual([], window._speech_queue)
            self.assertEqual(8, window._voice_turn)
            self.assertEqual(4, window._response_generation)

        asyncio.run(run())



if __name__ == "__main__":
    unittest.main()
