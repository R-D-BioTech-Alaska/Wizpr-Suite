from __future__ import annotations

import unittest

from wizpr_suite.core.config import AppConfig
from wizpr_suite.ui.main_window import AUTO_VOICE_DUPLICATE_WINDOW_SECONDS, MainWindow


class VoiceRoutingTests(unittest.TestCase):
    def _window(self) -> MainWindow:
        window = MainWindow.__new__(MainWindow)
        window.cfg = AppConfig()
        window._last_auto_voice_signature = None
        window._last_auto_voice_at = 0.0
        return window

    def test_codex_wake_phrase_strips_to_command(self) -> None:
        window = self._window()

        command = window._voice_command_for_target("Codex, open the Ollama files", "codex")

        self.assertEqual("open the Ollama files", command)

    def test_default_wake_phrase_aliases_cover_common_transcription_misses(self) -> None:
        window = self._window()

        self.assertEqual("open the Ollama files", window._voice_command_for_target("Code X, open the Ollama files", "codex"))
        self.assertEqual("check the latest capture", window._voice_command_for_target("Code, check the latest capture", "codex"))
        self.assertEqual("summarize this", window._voice_command_for_target("Whisper, summarize this", "assistant"))
        self.assertEqual("summarize this", window._voice_command_for_target("summarize this", "assistant"))
        self.assertEqual("check the provider", window._voice_command_for_target("Open Code, check the provider", "opencode"))

    def test_custom_wake_phrase_does_not_keep_default_aliases(self) -> None:
        window = self._window()
        window.cfg.transcription.codex_wake_word = "Computer"

        self.assertEqual("", window._voice_command_for_target("Code X, open the files", "codex"))
        self.assertEqual("open the files", window._voice_command_for_target("Computer, open the files", "codex"))

    def test_missing_coding_wake_phrase_holds_transcript(self) -> None:
        window = self._window()

        command = window._voice_command_for_target("open the Ollama files", "codex")

        self.assertEqual("", command)

    def test_app_action_targets_still_require_wake_when_global_wake_is_off(self) -> None:
        window = self._window()
        window.cfg.transcription.require_wake_word = False

        self.assertEqual("open the Ollama files", window._voice_command_for_target("open the Ollama files", "assistant"))
        self.assertEqual("", window._voice_command_for_target("open Notepad", "codex"))
        self.assertEqual("", window._voice_command_for_target("open Notepad", "opencode"))
        self.assertEqual("", window._voice_command_for_target("paste this", "paste"))
        self.assertEqual("", window._voice_command_for_target("copy this", "clipboard"))
        self.assertEqual("open Notepad", window._voice_command_for_target("Codex, open Notepad", "codex"))

    def test_transcript_target_does_not_need_wake_phrase(self) -> None:
        window = self._window()

        command = window._voice_command_for_target("plain transcript", "transcript")

        self.assertEqual("plain transcript", command)

    def test_clipboard_and_paste_targets_use_wizpr_wake_phrase(self) -> None:
        window = self._window()

        self.assertEqual("copy this", window._voice_command_for_target("Wizpr, copy this", "clipboard"))
        self.assertEqual("paste this", window._voice_command_for_target("Whisper, paste this", "paste"))
        self.assertEqual("", window._voice_command_for_target("paste this", "paste"))

    def test_coding_voice_desktop_app_commands_need_review(self) -> None:
        window = self._window()

        self.assertTrue(window._coding_voice_command_needs_review("open Notepad"))
        self.assertTrue(window._coding_voice_command_needs_review("launch Chrome"))
        self.assertTrue(window._coding_voice_command_needs_review("press control v"))
        self.assertFalse(window._coding_voice_command_needs_review("check the Ollama provider files"))

    def test_wake_required_allows_short_confirmed_command(self) -> None:
        window = self._window()
        window.cfg.transcription.require_wake_word = True

        self.assertEqual("", window._auto_voice_command_rejection_reason("stop"))

    def test_assistant_accepts_one_word_conversation(self) -> None:
        window = self._window()
        window.cfg.transcription.require_wake_word = False

        self.assertTrue(window._auto_voice_command_ready("assistant", "stop"))

    def test_duplicate_voice_command_window_uses_normalized_text(self) -> None:
        window = self._window()

        window._remember_auto_voice_command("codex", "Open the Ollama files.", now=10.0)

        self.assertTrue(window._is_duplicate_auto_voice_command("codex", "open the ollama files", now=11.0))
        self.assertFalse(
            window._is_duplicate_auto_voice_command(
                "codex",
                "open the ollama files",
                now=10.0 + AUTO_VOICE_DUPLICATE_WINDOW_SECONDS + 0.1,
            )
        )
        self.assertFalse(window._is_duplicate_auto_voice_command("opencode", "open the ollama files", now=11.0))


if __name__ == "__main__":
    unittest.main()
