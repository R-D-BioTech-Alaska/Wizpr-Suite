from __future__ import annotations

import os
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtGui, QtWidgets

from wizpr_suite.ble.ble_manager import DiscoveredDevice
from wizpr_suite.core.config import CONFIG_FILE, load_config
from wizpr_suite.llm.base import LLMResponse
from wizpr_suite.ui import main_window
from wizpr_suite.ui.main_window import MainWindow


class LazyUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def _window(self) -> MainWindow:
        self.tmp = tempfile.TemporaryDirectory()
        return MainWindow(Path(self.tmp.name))

    def tearDown(self) -> None:
        tmp = getattr(self, "tmp", None)
        if tmp is not None:
            tmp.cleanup()

    def test_default_startup_builds_simple_tabs_only(self) -> None:
        win = self._window()
        try:
            tabs = [win.tabs.tabText(i) for i in range(win.tabs.count())]

            self.assertEqual(["Ring", "Talk"], tabs)
            self.assertFalse(hasattr(win, "llm_tabs"))
            self.assertFalse(hasattr(win, "map_table"))
            self.assertFalse(hasattr(win, "ble_table"))
            self.assertFalse(hasattr(win, "gatt_tree"))
            self.assertFalse(hasattr(win, "notify_box"))
            self.assertFalse(win._advanced_ble_built)
            self.assertIn("Ring not saved", win.quick_ready_label.text())
            self.assertIn("Assistant | wake off | speech on | interrupt phrase | automatic local STT small.en", win.quick_ready_label.text())
            self.assertIn("Bridge off", win.quick_ready_label.text())
            self.assertIsNotNone(win._warm_task)
        finally:
            win.close()


    def test_reference_inspired_shell_and_settings_are_available(self) -> None:
        win = self._window()
        try:
            self.assertEqual(205, win.sidebar_buttons["chat"].parentWidget().width())
            self.assertTrue(win.sidebar_buttons["chat"].isChecked())
            self.assertTrue(hasattr(win, "voice_waveform"))
            self.assertEqual("Conversation", win.findChild(QtWidgets.QLabel, "sectionTitle").text())
            self.assertEqual(9, win.settings_nav.count())
            win._open_settings("voice")
            self.assertEqual(win._settings_rows["voice"], win.settings_nav.currentRow())
            self.assertTrue(win.settings_dialog.isVisible())
        finally:
            win.close()


    def test_sidebar_connection_status_updates_without_hidden_page_dependency(self) -> None:
        win = self._window()
        try:
            win._set_ring_connection_status("Connected", "connected")
            self.assertEqual("Ring Connected", win.sidebar_ring_status.text())
            self.assertTrue(win.sidebar_ring_status.property("connected"))
            self.assertEqual("Connected", win._ring_connection_text)
            self.assertEqual("connected", win._ring_connection_state)
        finally:
            win.close()

    def test_mic_button_does_not_create_fake_recording_state(self) -> None:
        win = self._window()
        try:
            class ConnectedClient:
                is_connected = True

            win.ble._client = ConnectedClient()
            win._set_ring_connection_status("Connected", "connected")
            win._handle_mic_button()
            self.assertFalse(win._voice_ui_active)
            self.assertFalse(win.voice_waveform._active)
            self.assertIn("press the ring button", win.chat_voice_status_label.text().lower())
        finally:
            win.close()

    def test_connected_ring_lock_action_is_ignored_when_protected(self) -> None:
        win = self._window()
        try:
            class ConnectedClient:
                is_connected = True

            win.ble._client = ConnectedClient()
            win.cfg.protect_connected_ring_buttons = True
            called: list[str] = []

            async def fake_lock() -> None:
                called.append("lock")

            win.ring.lock = fake_lock
            win.loop.run_until_complete(win._toggle_ring_lock_from_button())
            self.assertEqual([], called)
        finally:
            win.close()

    def test_conversation_view_keeps_plain_text_compatibility(self) -> None:
        win = self._window()
        try:
            win.output.appendPlainText("> [OpenAI: model] hello")
            win._append_output_text("Hi there")
            self.assertIn("hello", win.output.toPlainText())
            self.assertIn("Hi there", win.output.toPlainText())
        finally:
            win.close()

    def test_memory_tools_and_interrupt_controls_are_available(self) -> None:
        win = self._window()
        try:
            self.assertTrue(win.memory_enabled_check.isChecked())
            self.assertEqual("ask", win.tool_permission_combo.currentData())
            self.assertEqual("word", win.interrupt_mode_combo.currentData())
            self.assertEqual("stop", win.interrupt_word_edit.text())
            self.assertFalse(win.run_tool_btn.isEnabled())
        finally:
            win.close()

    def test_top_bar_keeps_provider_controls_resizable(self) -> None:
        win = self._window()
        try:
            self.assertGreaterEqual(win.active_llm_combo.minimumContentsLength(), 16)
            self.assertEqual(
                QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon,
                win.active_llm_combo.sizeAdjustPolicy(),
            )
            self.assertTrue(win.active_llm_detail.wordWrap())
            self.assertTrue(win.quick_ready_label.wordWrap())
            self.assertTrue(win.next_step_label.wordWrap())
            self.assertFalse(win._advanced_tabs_built)
        finally:
            win.close()

    def test_next_step_line_tracks_ring_and_voice_state(self) -> None:
        win = self._window()
        try:
            self.assertIn("Auto Connect Ring", win.next_step_label.text())
            self.assertIn("press the ring button", win.next_step_label.text())

            win.cfg.last_ble_address = "AA:BB:CC:DD:EE:01"
            win._update_saved_ring_status()
            self.assertIn("saved-ring listener", win.next_step_label.text())

            win.cfg.transcription.require_wake_word = False
            win._set_ring_voice_target("codex")
            win._set_ring_connection_status("Connected", "connected")
            self.assertIn("raise the ring and speak", win.next_step_label.text())
            self.assertIn("start with 'Codex'", win.next_step_label.text())

            win.wake_phrase_edit.setText("Computer")
            win._simple_wake_phrase_changed()
            self.assertIn("start with 'Computer'", win.next_step_label.text())
        finally:
            win.close()

    def test_voice_warmup_startup_task_runs_for_default_local_backend(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(
                json.dumps({"transcription": {"warm_at_startup": True}}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"OPENAI_API_KEY": "", "WIZPR_LOCAL_WHISPER_BACKEND": "server"}, clear=False):
                win = MainWindow(app_dir)
                try:
                    self.assertIsNotNone(win._warm_task)
                    self.assertFalse(win._warm_task.done())
                finally:
                    win.close()

        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(
                json.dumps({"transcription": {"warm_at_startup": True}}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"OPENAI_API_KEY": "key", "WIZPR_LOCAL_WHISPER_BACKEND": "server"}, clear=False):
                win = MainWindow(app_dir)
                try:
                    self.assertIsNone(win._warm_task)
                finally:
                    win.close()

        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(
                json.dumps({"transcription": {"warm_at_startup": True, "stt_backend": "openai"}}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"OPENAI_API_KEY": "key"}, clear=False):
                win = MainWindow(app_dir)
                try:
                    self.assertIsNone(win._warm_task)
                finally:
                    win.close()

    def test_bridge_autostart_does_not_build_advanced_tabs(self) -> None:
        async def fake_start(self, show_status: bool = True) -> None:
            return None

        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(
                json.dumps({"mobile_bridge": {"enabled": True}}),
                encoding="utf-8",
            )
            with patch.object(MainWindow, "_start_mobile_bridge", fake_start):
                win = MainWindow(app_dir)
                try:
                    win.loop.run_until_complete(asyncio.sleep(0))
                    tabs = [win.tabs.tabText(i) for i in range(win.tabs.count())]

                    self.assertEqual(["Ring", "Talk"], tabs)
                    self.assertFalse(hasattr(win, "llm_tabs"))
                    self.assertFalse(win._advanced_tabs_built)
                finally:
                    win.close()

    def test_saved_ring_auto_connect_schedules_without_advanced_tabs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(
                json.dumps({"last_ble_address": "AA:BB:CC:DD:EE:01"}),
                encoding="utf-8",
            )
            win = MainWindow(app_dir)
            try:
                calls: list[str] = []

                async def fake_startup_connect() -> None:
                    calls.append("startup")

                win._connect_saved_ring_at_startup = fake_startup_connect
                win._schedule_saved_ring_auto_connect()
                win.loop.run_until_complete(asyncio.sleep(0))

                self.assertEqual(["startup"], calls)
                self.assertFalse(win._advanced_tabs_built)
            finally:
                win.close()

    def test_saved_ring_auto_connect_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(
                json.dumps({"last_ble_address": "AA:BB:CC:DD:EE:01", "auto_connect_saved_ring": False}),
                encoding="utf-8",
            )
            win = MainWindow(app_dir)
            try:
                calls: list[str] = []

                async def fake_startup_connect() -> None:
                    calls.append("startup")

                win._connect_saved_ring_at_startup = fake_startup_connect
                win._schedule_saved_ring_auto_connect()
                win.loop.run_until_complete(asyncio.sleep(0))

                self.assertEqual([], calls)
                self.assertFalse(win.ring_auto_start_check.isChecked())
            finally:
                win.close()

    def test_saved_ring_waiting_state_updates_simple_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(
                json.dumps({"last_ble_address": "AA:BB:CC:DD:EE:01"}),
                encoding="utf-8",
            )
            win = MainWindow(app_dir)
            try:
                started = asyncio.Event()
                release = asyncio.Event()

                async def fake_startup_connect() -> None:
                    started.set()
                    await release.wait()

                win._connect_saved_ring_at_startup = fake_startup_connect
                win._schedule_saved_ring_auto_connect()
                win.loop.run_until_complete(asyncio.wait_for(started.wait(), timeout=1.0))

                self.assertTrue(win._saved_ring_auto_connect_running())
                self.assertFalse(win.wizpr_auto_btn.isEnabled())
                self.assertTrue(win.ble_disconnect_btn.isEnabled())

                win._cancel_saved_ring_auto_connect()
                win.loop.run_until_complete(asyncio.sleep(0))

                self.assertFalse(win._saved_ring_auto_connect_running())
                self.assertTrue(win.wizpr_auto_btn.isEnabled())
                self.assertFalse(win.ble_disconnect_btn.isEnabled())
                release.set()
            finally:
                win.close()

    def test_saved_ring_listener_keeps_waiting_until_ring_appears(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(
                json.dumps({"last_ble_address": "AA:BB:CC:DD:EE:01"}),
                encoding="utf-8",
            )
            win = MainWindow(app_dir)
            try:
                scan_windows: list[float] = []
                connect_calls: list[tuple[str, bool]] = []

                async def fake_scan(address: str, timeout: float = 25.0) -> DiscoveredDevice | None:
                    scan_windows.append(timeout)
                    if len(scan_windows) == 1:
                        return None
                    return DiscoveredDevice(
                        "AA:BB:CC:DD:EE:01",
                        "WIZPR RING-EE:01",
                        -42,
                        [main_window.WIZPR_RING_SERVICE_UUID],
                    )

                async def fake_connect(address: str, timeout: float = 18.0, quick: bool = False) -> bool:
                    connect_calls.append((address, quick))
                    if quick:
                        win._set_ring_connection_status("Connected", "connected")
                    return quick

                win._scan_for_saved_ring = fake_scan
                win._connect_remembered_ring = fake_connect
                with patch("wizpr_suite.ui.main_window.SAVED_RING_RETRY_DELAY_SECONDS", 0.0):
                    win.loop.run_until_complete(win._connect_saved_ring_at_startup())

                self.assertEqual(
                    [main_window.SAVED_RING_STARTUP_SCAN_SECONDS, main_window.SAVED_RING_RETRY_SCAN_SECONDS],
                    scan_windows,
                )
                self.assertEqual(
                    [("AA:BB:CC:DD:EE:01", False), ("AA:BB:CC:DD:EE:01", True)],
                    connect_calls,
                )
                self.assertEqual("Connected", win.ring_connection_status.text())
            finally:
                win.close()

    def test_forget_saved_ring_cancels_startup_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(
                json.dumps({"last_ble_address": "AA:BB:CC:DD:EE:01"}),
                encoding="utf-8",
            )
            win = MainWindow(app_dir)
            try:
                started = asyncio.Event()
                release = asyncio.Event()

                async def fake_startup_connect() -> None:
                    started.set()
                    await release.wait()

                win._connect_saved_ring_at_startup = fake_startup_connect
                win._schedule_saved_ring_auto_connect()
                win.loop.run_until_complete(asyncio.wait_for(started.wait(), timeout=1.0))

                win.forget_ring_btn.click()
                win.loop.run_until_complete(asyncio.sleep(0))

                self.assertEqual("", win.cfg.last_ble_address)
                self.assertFalse(win._saved_ring_auto_connect_running())
                self.assertTrue(win.wizpr_auto_btn.isEnabled())
                self.assertFalse(win.ble_disconnect_btn.isEnabled())
                release.set()
            finally:
                win.close()

    def test_talk_voice_target_does_not_need_mappings_tab_built(self) -> None:
        win = self._window()
        try:
            win._set_ring_voice_target("paste")

            self.assertEqual("paste", win.cfg.ring_voice_target)
            self.assertIn("audio_capture", win.cfg.mappings["paste_audio_to_active_app"])
            self.assertNotIn("audio_capture", win.cfg.mappings["send_audio_to_assistant"])
            self.assertFalse(hasattr(win, "map_table"))
        finally:
            win.close()

    def test_simple_wake_required_toggle_saves_without_advanced(self) -> None:
        win = self._window()
        try:
            self.assertFalse(win.cfg.transcription.require_wake_word)

            win.wake_required_check.setChecked(True)

            self.assertTrue(win.cfg.transcription.require_wake_word)
            self.assertIn("wake phrase required", win.voice_status_label.text())
            self.assertFalse(hasattr(win, "transcription_require_wake"))
            self.assertFalse(win._advanced_tabs_built)
            self.assertTrue(load_config(Path(self.tmp.name)).transcription.require_wake_word)
            self.assertIn("wake on", win.quick_ready_label.text())
        finally:
            win.close()

    def test_protected_voice_targets_show_forced_wake_without_changing_global_setting(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(
                json.dumps(
                    {
                        "ring_voice_target": "codex",
                        "transcription": {"require_wake_word": False},
                    }
                ),
                encoding="utf-8",
            )
            win = MainWindow(app_dir)
            try:
                self.assertEqual("codex", win.cfg.ring_voice_target)
                self.assertFalse(win.cfg.transcription.require_wake_word)
                self.assertTrue(win.wake_required_check.isChecked())
                self.assertFalse(win.wake_required_check.isEnabled())
                self.assertIn("Codex wake phrase required", win.voice_status_label.text())
                self.assertIn("wake on", win.quick_ready_label.text())

                win._save_transcription()

                cfg = load_config(app_dir)
                self.assertFalse(cfg.transcription.require_wake_word)
                self.assertEqual("codex", cfg.ring_voice_target)
            finally:
                win.close()

    def test_quick_ready_line_tracks_simple_voice_changes(self) -> None:
        win = self._window()
        try:
            win._set_ring_voice_target("opencode")
            win.speak_responses_check.setChecked(False)

            text = win.quick_ready_label.text()
            self.assertIn("OpenCode", text)
            self.assertIn("speech off", text)
            self.assertFalse(win._advanced_tabs_built)
        finally:
            win.close()

    def test_simple_wake_phrase_saves_without_advanced(self) -> None:
        win = self._window()
        try:
            win._set_ring_voice_target("codex")
            win.wake_phrase_edit.setText("Computer")
            win._simple_wake_phrase_changed()

            cfg = load_config(Path(self.tmp.name))
            self.assertEqual("Computer", cfg.transcription.codex_wake_word)
            self.assertFalse(win._advanced_tabs_built)
        finally:
            win.close()

    def test_simple_wake_phrase_follows_voice_target(self) -> None:
        win = self._window()
        try:
            self.assertEqual("Wizpr, Assistant", win.wake_phrase_edit.text())

            win._set_ring_voice_target("codex")
            self.assertEqual("Codex", win.wake_phrase_edit.text())

            win.wake_phrase_edit.setText("Computer")
            win._simple_wake_phrase_changed()
            win._set_ring_voice_target("assistant")
            self.assertEqual("Wizpr, Assistant", win.wake_phrase_edit.text())
            win._set_ring_voice_target("codex")
            self.assertEqual("Computer", win.wake_phrase_edit.text())
        finally:
            win.close()

    def test_wake_phrase_stays_synced_with_advanced_voice_page(self) -> None:
        win = self._window()
        try:
            win._set_ring_voice_target("opencode")
            win._advanced_changed(True)

            win.wake_phrase_edit.setText("Worker")
            win._simple_wake_phrase_changed()
            self.assertEqual("Worker", win.transcription_opencode_wake.text())

            win.transcription_opencode_wake.setText("Open Sesame")
            win._save_transcription()
            self.assertEqual("Open Sesame", win.wake_phrase_edit.text())
        finally:
            win.close()

    def test_transcript_only_disables_simple_wake_phrase(self) -> None:
        win = self._window()
        try:
            win._set_ring_voice_target("transcript")

            self.assertFalse(win.wake_phrase_edit.isEnabled())
            self.assertEqual("", win.wake_phrase_edit.text())
        finally:
            win.close()

    def test_clipboard_and_paste_voice_targets_have_simple_wake_phrases(self) -> None:
        win = self._window()
        try:
            clipboard_idx = win.ring_voice_target.findData("clipboard")
            paste_idx = win.ring_voice_target.findData("paste")
            self.assertGreaterEqual(clipboard_idx, 0)
            self.assertGreaterEqual(paste_idx, 0)

            win._set_ring_voice_target("clipboard")
            self.assertEqual("Copy Text Wake:", win.wake_phrase_label.text())
            self.assertEqual("Wizpr", win.wake_phrase_edit.text())

            win.wake_phrase_edit.setText("Computer")
            win._simple_wake_phrase_changed()
            self.assertEqual("Computer", load_config(Path(self.tmp.name)).transcription.clipboard_wake_word)

            self.assertEqual("Copy Text", win.ring_voice_target.itemText(clipboard_idx))
            self.assertEqual("Voice Keyboard", win.ring_voice_target.itemText(paste_idx))
        finally:
            win.close()

    def test_simple_ring_settings_save_without_advanced(self) -> None:
        win = self._window()
        try:
            voice_idx = win.ring_voice_mode.findData("all")
            sleep_idx = win.ring_sleep_timeout.findData(10)
            self.assertGreaterEqual(voice_idx, 0)
            self.assertGreaterEqual(sleep_idx, 0)

            win.ring_voice_mode.setCurrentIndex(voice_idx)
            win.ring_sleep_timeout.setCurrentIndex(sleep_idx)
            win.ring_tts_response_check.setChecked(False)
            win.ring_auto_start_check.setChecked(False)
            win.ring_connect_sound_check.setChecked(False)
            win.ring_mic_sound_check.setChecked(False)
            win.ring_low_battery_check.setChecked(False)

            cfg = load_config(Path(self.tmp.name))
            self.assertEqual("all", cfg.transcription.voice_mode)
            self.assertEqual(10, cfg.transcription.ring_sleep_timeout_seconds)
            self.assertFalse(cfg.transcription.speak_responses)
            self.assertFalse(cfg.auto_connect_saved_ring)
            self.assertFalse(cfg.transcription.ring_connection_sound)
            self.assertFalse(cfg.transcription.mic_activation_sound)
            self.assertFalse(cfg.transcription.low_battery_warning)
            self.assertFalse(win.speak_responses_check.isChecked())
            self.assertFalse(win._advanced_tabs_built)
        finally:
            win.close()

    def test_simple_button_mode_switches_presets_without_advanced(self) -> None:
        win = self._window()
        try:
            self.assertEqual("app", win.ring_button_mode.currentData())
            self.assertIn("connection protected", win.ring_button_summary.text())
            self.assertNotIn("button_single", win.cfg.mappings["toggle_ring_lock"])

            coding_idx = win.ring_button_mode.findData("coding")
            self.assertGreaterEqual(coding_idx, 0)
            win.ring_button_mode.setCurrentIndex(coding_idx)

            cfg = load_config(Path(self.tmp.name))
            self.assertEqual("coding", cfg.button_mode)
            self.assertIn("button_single", cfg.mappings["toggle_listen"])
            self.assertIn("button_double", cfg.mappings["send_last_transcript"])
            self.assertIn("button_triple", cfg.mappings["send_last_to_codex"])
            self.assertNotIn("button_single", cfg.mappings["toggle_ring_lock"])
            self.assertIn("1 Listen", win.ring_button_summary.text())
            self.assertFalse(win._advanced_tabs_built)
        finally:
            win.close()

    def test_advanced_button_mapping_marks_simple_mode_custom(self) -> None:
        win = self._window()
        try:
            win._advanced_changed(True)
            win.map_trigger.setText("button_single")
            idx = win.map_action.findText("copy_last_transcript")
            self.assertGreaterEqual(idx, 0)
            win.map_action.setCurrentIndex(idx)

            win._add_mapping()

            cfg = load_config(Path(self.tmp.name))
            self.assertEqual("custom", cfg.button_mode)
            self.assertEqual("custom", win.ring_button_mode.currentData())
            self.assertIn("custom", win.ring_button_summary.text())
        finally:
            win.close()

    def test_app_style_button_helpers_are_local_and_simple(self) -> None:
        win = self._window()
        try:
            win.prompt.setPlainText("old prompt")
            win.output.setPlainText("old answer")
            win._last_response_text = "old answer"
            win.replay_response_btn.setEnabled(True)

            win._start_new_chat_from_button()

            self.assertEqual("", win.prompt.toPlainText())
            self.assertEqual("", win.output.toPlainText())
            self.assertFalse(win.replay_response_btn.isEnabled())

            win._last_transcript = "fix this sentence"
            win._edit_last_transcript_from_button()

            self.assertEqual("fix this sentence", win.prompt.toPlainText())
        finally:
            win.close()

    def test_ring_settings_stay_synced_with_advanced_voice_page(self) -> None:
        win = self._window()
        try:
            win._advanced_changed(True)

            voice_idx = win.ring_voice_mode.findData("all")
            sleep_idx = win.ring_sleep_timeout.findData(10)
            win.ring_voice_mode.setCurrentIndex(voice_idx)
            win.ring_sleep_timeout.setCurrentIndex(sleep_idx)

            self.assertEqual("all", win.transcription_voice_mode.currentData())
            self.assertEqual(10, win.transcription_sleep_timeout.currentData())

            advanced_voice_idx = win.transcription_voice_mode.findData("proximity")
            advanced_sleep_idx = win.transcription_sleep_timeout.findData(3)
            win.transcription_voice_mode.setCurrentIndex(advanced_voice_idx)
            win.transcription_sleep_timeout.setCurrentIndex(advanced_sleep_idx)
            win.speak_responses_check.setChecked(False)
            win.ring_connection_sound.setChecked(False)
            win.mic_activation_sound.setChecked(False)
            win.low_battery_warning.setChecked(False)
            win._save_transcription()

            self.assertEqual("proximity", win.ring_voice_mode.currentData())
            self.assertEqual(3, win.ring_sleep_timeout.currentData())
            self.assertFalse(win.ring_tts_response_check.isChecked())
            self.assertFalse(win.ring_connect_sound_check.isChecked())
            self.assertFalse(win.ring_mic_sound_check.isChecked())
            self.assertFalse(win.ring_low_battery_check.isChecked())
        finally:
            win.close()

    def test_advanced_voice_page_preserves_zero_preflight_active_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(
                json.dumps(
                    {
                        "show_advanced_options": True,
                        "transcription": {"audio_preflight_min_active_seconds": 0.18},
                    }
                ),
                encoding="utf-8",
            )
            win = MainWindow(app_dir)
            try:
                self.assertEqual(0.0, win.cfg.transcription.audio_preflight_min_active_seconds)
                self.assertEqual(0.0, win.audio_preflight_min_active.value())
                win._save_transcription()
                self.assertEqual(0.0, load_config(app_dir).transcription.audio_preflight_min_active_seconds)
            finally:
                win.close()

    def test_talk_prompt_and_output_are_resizable(self) -> None:
        win = self._window()
        try:
            self.assertTrue(hasattr(win, "chat_split"))
            self.assertEqual(2, win.chat_split.count())
            self.assertFalse(win.chat_split.childrenCollapsible())
            self.assertEqual(QtWidgets.QPlainTextEdit.WidgetWidth, win.prompt.lineWrapMode())
            self.assertEqual(QtWidgets.QPlainTextEdit.WidgetWidth, win.output.lineWrapMode())
        finally:
            win.close()

    def test_talk_output_can_be_cleared(self) -> None:
        win = self._window()
        try:
            win.output.setPlainText("old reply")
            win.clear_output_btn.click()

            self.assertEqual("", win.output.toPlainText())
        finally:
            win.close()

    def test_talk_action_buttons_follow_prompt_and_transcript(self) -> None:
        win = self._window()
        try:
            self.assertFalse(win.send_btn.isEnabled())
            self.assertFalse(win.clear_btn.isEnabled())
            self.assertFalse(win.send_last_btn.isEnabled())
            self.assertFalse(win.send_codex_btn.isEnabled())
            self.assertFalse(win.send_opencode_btn.isEnabled())
            self.assertFalse(win.copy_text_btn.isEnabled())

            win.prompt.setPlainText("hello")

            self.assertTrue(win.send_btn.isEnabled())
            self.assertTrue(win.clear_btn.isEnabled())
            self.assertTrue(win.send_codex_btn.isEnabled())
            self.assertTrue(win.send_opencode_btn.isEnabled())
            self.assertTrue(win.copy_text_btn.isEnabled())
            self.assertFalse(win.send_last_btn.isEnabled())

            win.prompt.clear()
            win._last_transcript = "saved transcript"
            win._update_talk_action_state()

            self.assertFalse(win.send_btn.isEnabled())
            self.assertFalse(win.clear_btn.isEnabled())
            self.assertTrue(win.send_last_btn.isEnabled())
            self.assertTrue(win.send_codex_btn.isEnabled())
            self.assertTrue(win.send_opencode_btn.isEnabled())
            self.assertTrue(win.copy_text_btn.isEnabled())
        finally:
            win.close()

    def test_copy_text_uses_prompt_then_last_transcript(self) -> None:
        win = self._window()
        try:
            clipboard = QtWidgets.QApplication.clipboard()

            win.prompt.setPlainText("prompt text")
            win._last_transcript = "transcript text"
            win.copy_text_btn.click()

            self.assertEqual("prompt text", clipboard.text())
            self.assertIn("Prompt copied", win.statusBar().currentMessage())

            win.prompt.clear()
            win._update_talk_action_state()
            win.copy_text_btn.click()

            self.assertEqual("transcript text", clipboard.text())
            self.assertIn("Transcript copied", win.statusBar().currentMessage())
        finally:
            win.close()

    def test_paste_last_transcript_action_copies_then_sends_paste_hotkey(self) -> None:
        win = self._window()
        try:
            calls: list[str] = []

            async def fake_paste() -> None:
                calls.append("paste")

            win._last_transcript = "dictated text"
            win._send_paste_hotkey = fake_paste

            win.loop.run_until_complete(win.router.dispatch("paste_last_transcript", {}))

            self.assertEqual("dictated text", QtWidgets.QApplication.clipboard().text())
            self.assertEqual(["paste"], calls)
            self.assertIn("pasted", win.statusBar().currentMessage())
        finally:
            win.close()

    def test_ring_audio_paste_target_uses_current_capture_text(self) -> None:
        win = self._window()
        try:
            audio = Path(self.tmp.name) / "voice.wav"
            audio.write_bytes(b"fake")
            calls: list[str] = []

            async def fake_transcribe(path: Path) -> str:
                calls.append(path.name)
                return "Wizpr, dictated text"

            async def fake_paste() -> None:
                calls.append("paste")

            win._transcribe_audio_file = fake_transcribe
            win._send_paste_hotkey = fake_paste

            win.loop.run_until_complete(win._paste_audio_capture_to_active_app({"path": str(audio)}))

            self.assertEqual(["voice.wav", "paste"], calls)
            self.assertEqual("dictated text", QtWidgets.QApplication.clipboard().text())
            self.assertEqual("Wizpr, dictated text", win._last_transcript)
            self.assertEqual("dictated text", win.prompt.toPlainText())
            self.assertIn("[voice paste] dictated text", win.output.toPlainText())
        finally:
            win.close()

    def test_mobile_bridge_clipboard_and_paste_targets_use_desktop_helpers(self) -> None:
        win = self._window()
        try:
            calls: list[str] = []

            async def fake_paste() -> None:
                calls.append("paste")

            win._send_paste_hotkey = fake_paste

            copied = win.loop.run_until_complete(
                win._handle_mobile_bridge_command({"target": "clipboard", "text": "phone copy"})
            )
            pasted = win.loop.run_until_complete(
                win._handle_mobile_bridge_command({"target": "paste", "text": "phone paste"})
            )

            self.assertTrue(copied["ok"])
            self.assertEqual("Copied to clipboard.", copied["text"])
            self.assertTrue(pasted["ok"])
            self.assertEqual("Pasted into active app.", pasted["text"])
            self.assertEqual("phone paste", QtWidgets.QApplication.clipboard().text())
            self.assertEqual("phone paste", win._last_transcript)
            self.assertEqual(["paste"], calls)
            self.assertIn("pasted", win.statusBar().currentMessage())
        finally:
            win.close()

    def test_duplicate_ring_voice_command_is_not_sent_twice(self) -> None:
        win = self._window()
        try:
            audio = Path(self.tmp.name) / "voice.wav"
            audio.write_bytes(b"fake")
            sent: list[str] = []

            async def fake_transcribe(_path: Path) -> str:
                return "what is the battery status"

            async def fake_send(command: str) -> None:
                sent.append(command)

            win._transcribe_audio_file = fake_transcribe
            win._send_prompt_to_assistant = fake_send

            win.loop.run_until_complete(win._send_audio_capture_to_assistant({"path": str(audio)}))
            win.loop.run_until_complete(win._send_audio_capture_to_assistant({"path": str(audio)}))

            self.assertEqual(["what is the battery status"], sent)
            self.assertIn("Duplicate Assistant command skipped", win.output.toPlainText())
            self.assertIn("duplicate Assistant", win.voice_status_label.text())
        finally:
            win.close()

    def test_wake_free_assistant_desktop_command_is_held_for_review(self) -> None:
        win = self._window()
        try:
            audio = Path(self.tmp.name) / "voice.wav"
            audio.write_bytes(b"fake")
            sent: list[str] = []
            win.cfg.transcription.require_wake_word = False

            async def fake_transcribe(_path: Path) -> str:
                return "open Notepad"

            async def fake_send(command: str) -> None:
                sent.append(command)

            win._transcribe_audio_file = fake_transcribe
            win._send_prompt_to_assistant = fake_send

            win.loop.run_until_complete(win._send_audio_capture_to_assistant({"path": str(audio)}))

            self.assertEqual([], sent)
            self.assertEqual("open Notepad", win.prompt.toPlainText())
            self.assertIn("tool approval", win.output.toPlainText())
            self.assertIn("Click Run Tool to approve", win.output.toPlainText())
        finally:
            win.close()

    def test_wake_free_assistant_normal_question_still_sends(self) -> None:
        win = self._window()
        try:
            audio = Path(self.tmp.name) / "voice.wav"
            audio.write_bytes(b"fake")
            sent: list[str] = []
            win.cfg.transcription.require_wake_word = False

            async def fake_transcribe(_path: Path) -> str:
                return "what is the battery status"

            async def fake_send(command: str) -> None:
                sent.append(command)

            win._transcribe_audio_file = fake_transcribe
            win._send_prompt_to_assistant = fake_send

            win.loop.run_until_complete(win._send_audio_capture_to_assistant({"path": str(audio)}))

            self.assertEqual(["what is the battery status"], sent)
        finally:
            win.close()

    def test_coding_ring_voice_desktop_command_is_held_even_when_auto_send_enabled(self) -> None:
        win = self._window()
        try:
            audio = Path(self.tmp.name) / "voice.wav"
            audio.write_bytes(b"fake")
            sent: list[str] = []
            win.cfg.transcription.hold_coding_voice_commands = False

            async def fake_transcribe(_path: Path) -> str:
                return "Codex, open Notepad"

            async def fake_send(command: str) -> None:
                sent.append(command)

            win._transcribe_audio_file = fake_transcribe
            win._send_prompt_to_codex = fake_send

            win.loop.run_until_complete(win._send_audio_capture_to_codex({"path": str(audio)}))

            self.assertEqual([], sent)
            self.assertEqual("open Notepad", win.prompt.toPlainText())
            self.assertIn("Desktop/app-control voice command needs review", win.output.toPlainText())
        finally:
            win.close()

    def test_same_audio_capture_transcribes_once(self) -> None:
        win = self._window()
        try:
            audio = Path(self.tmp.name) / "voice.wav"
            audio.write_bytes(b"fake")
            calls = 0
            win.cfg.transcription.audio_preflight_enabled = False

            async def fake_transcribe(_path: Path, model_name: str | None = None, compute_type: str | None = None):
                nonlocal calls
                calls += 1
                return "Wizpr, open Notepad", ""

            with patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
                with patch("wizpr_suite.ui.main_window.transcribe_audio_local", fake_transcribe):
                    first = win.loop.run_until_complete(win._transcribe_audio_file(audio))
                    second = win.loop.run_until_complete(win._transcribe_audio_file(audio))

            self.assertEqual("Wizpr, open Notepad", first)
            self.assertEqual(first, second)
            self.assertEqual(1, calls)
            self.assertIn("Voice heard", win.voice_status_label.text())
        finally:
            win.close()

    def test_openai_key_does_not_override_local_transcription_backend(self) -> None:
        win = self._window()
        try:
            audio = Path(self.tmp.name) / "voice.wav"
            audio.write_bytes(b"fake")
            calls: list[str] = []
            win.cfg.transcription.audio_preflight_enabled = False
            win.cfg.transcription.stt_backend = "local"

            async def fake_local(_path: Path, model_name: str | None = None, compute_type: str | None = None):
                calls.append("local")
                return "Wizpr, local transcript", ""

            async def fail_openai(*_args, **_kwargs):
                raise AssertionError("OpenAI transcription should not run when local is selected")

            win.p_openai.transcribe_audio = fail_openai
            with patch.dict(os.environ, {"OPENAI_API_KEY": "key"}, clear=False):
                with patch("wizpr_suite.ui.main_window.transcribe_audio_local", fake_local):
                    text = win.loop.run_until_complete(win._transcribe_audio_file(audio))

            self.assertEqual("Wizpr, local transcript", text)
            self.assertEqual(["local"], calls)
            self.assertIn("Voice heard", win.voice_status_label.text())
            self.assertNotIn("Using OpenAI transcription", win.output.toPlainText())
        finally:
            win.close()

    def test_local_transcription_timeout_reports_error_and_resets_worker(self) -> None:
        win = self._window()
        try:
            audio = Path(self.tmp.name) / "voice.wav"
            audio.write_bytes(b"fake")
            closed: list[str] = []
            win.cfg.transcription.audio_preflight_enabled = False
            win.cfg.transcription.stt_backend = "local"

            async def slow_local(*_args, **_kwargs):
                await asyncio.sleep(1.0)
                return "late", ""

            async def fake_close() -> None:
                closed.append("closed")

            with patch("wizpr_suite.ui.main_window.local_transcription_request_timeout_seconds", return_value=0.01):
                with patch("wizpr_suite.ui.main_window.transcribe_audio_local", slow_local):
                    with patch("wizpr_suite.ui.main_window.close_local_transcriber", fake_close):
                        text = win.loop.run_until_complete(win._transcribe_audio_file(audio))

            self.assertEqual("", text)
            self.assertEqual(["closed"], closed)
            self.assertIn("Local transcription timed out", "\n".join(win._ble_log_backlog))
            self.assertIn("Voice error", win.voice_status_label.text())
        finally:
            win.close()

    def test_openai_transcription_only_runs_when_selected(self) -> None:
        win = self._window()
        try:
            audio = Path(self.tmp.name) / "voice.wav"
            audio.write_bytes(b"fake")
            calls: list[str] = []
            win.cfg.transcription.audio_preflight_enabled = False
            win.cfg.transcription.stt_backend = "openai"

            async def fake_openai(_path: Path, model: str, prompt: str = ""):
                calls.append(model)
                return "Wizpr, cloud transcript", ""

            async def fail_local(*_args, **_kwargs):
                raise AssertionError("Local transcription should not run when OpenAI succeeds")

            win.p_openai.transcribe_audio = fake_openai
            with patch.dict(os.environ, {"OPENAI_API_KEY": "key"}, clear=False):
                with patch("wizpr_suite.ui.main_window.transcribe_audio_local", fail_local):
                    text = win.loop.run_until_complete(win._transcribe_audio_file(audio))

            self.assertEqual("Wizpr, cloud transcript", text)
            self.assertEqual([win.cfg.openai.transcription_model], calls)
            self.assertIn("voice: using OpenAI transcription", "\n".join(win._ble_log_backlog))
        finally:
            win.close()

    def test_ready_line_auto_backend_uses_openai_when_key_is_available(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "key"}, clear=False):
            win = self._window()
            try:
                self.assertIn("automatic OpenAI STT", win.quick_ready_label.text())
            finally:
                win.close()

    def test_enter_sends_prompt_and_shift_enter_keeps_newline(self) -> None:
        win = self._window()
        try:
            calls = []
            win._send_chat = lambda: calls.append("send")
            enter = QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_Return, QtCore.Qt.NoModifier)
            shift_enter = QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_Return, QtCore.Qt.ShiftModifier)

            self.assertTrue(win.eventFilter(win.prompt, enter))
            self.assertFalse(win.eventFilter(win.prompt, shift_enter))
            self.assertEqual(["send"], calls)
        finally:
            win.close()

    def test_simple_ring_activity_shows_status_events(self) -> None:
        win = self._window()
        try:
            handler = win.loop.run_until_complete(win._mk_notify_topic_handler("battery"))
            win.loop.run_until_complete(handler({"level": 55, "voltage": 3.52}))

            self.assertIn("Battery 55% (3.52V)", win.ring_activity_box.toPlainText())
            self.assertFalse(hasattr(win, "notify_box"))
        finally:
            win.close()

    def test_low_battery_warning_is_single_shot_until_recovered(self) -> None:
        win = self._window()
        try:
            handler = win.loop.run_until_complete(win._mk_notify_topic_handler("battery"))

            win.loop.run_until_complete(handler({"level": 15, "voltage": 3.30}))
            win.loop.run_until_complete(handler({"level": 14, "voltage": 3.29}))

            activity = win.ring_activity_box.toPlainText()
            self.assertEqual(1, activity.count("Low battery warning"))
            self.assertIn("Battery: 14% (3.29V) - low", win.ring_battery_status.text())
            self.assertIn("Ring low battery 14%", win.quick_ready_label.text())

            win.loop.run_until_complete(handler({"level": 20, "voltage": 3.34}))

            self.assertIn("Battery back above warning level: 20%", win.ring_activity_box.toPlainText())
            self.assertNotIn("low battery", win.quick_ready_label.text().casefold())
        finally:
            win.close()

    def test_low_battery_warning_can_be_disabled(self) -> None:
        win = self._window()
        try:
            win.ring_low_battery_check.setChecked(False)
            handler = win.loop.run_until_complete(win._mk_notify_topic_handler("battery"))

            win.loop.run_until_complete(handler({"level": 10, "voltage": 3.21}))

            self.assertNotIn("Low battery warning", win.ring_activity_box.toPlainText())
            self.assertEqual("Battery: 10% (3.21V)", win.ring_battery_status.text())
            self.assertNotIn("low battery", win.quick_ready_label.text().casefold())
        finally:
            win.close()

    def test_simple_ring_forget_button_tracks_saved_ring(self) -> None:
        win = self._window()
        try:
            self.assertFalse(win.forget_ring_btn.isEnabled())
            self.assertFalse(win.ble_disconnect_btn.isEnabled())

            win.cfg.last_ble_address = "AA:BB:CC:DD:EE:01"
            win._update_saved_ring_status()

            self.assertTrue(win.forget_ring_btn.isEnabled())
            self.assertFalse(win.ble_disconnect_btn.isEnabled())
            win.forget_ring_btn.click()
            self.assertEqual("", win.cfg.last_ble_address)
            self.assertFalse(win.forget_ring_btn.isEnabled())
        finally:
            win.close()

    def test_simple_ring_disconnect_button_follows_connection_state(self) -> None:
        win = self._window()
        try:
            self.assertFalse(win.ble_disconnect_btn.isEnabled())

            win._set_ring_connection_status("Connected", "connected")
            self.assertTrue(win.ble_disconnect_btn.isEnabled())

            win._set_ring_connection_status("Reconnecting", "connecting")
            self.assertTrue(win.ble_disconnect_btn.isEnabled())

            win._ring_keep_connected = False
            win._ring_connecting = False
            win._set_ring_connection_status("Disconnected", "disconnected")
            self.assertFalse(win.ble_disconnect_btn.isEnabled())
        finally:
            win.close()

    def test_spoken_responses_are_queued(self) -> None:
        spoken: list[str] = []

        async def fake_speak(text: str, voice: str = "", rate: int = 0) -> tuple[bool, str]:
            spoken.append(text)
            await asyncio.sleep(0)
            return True, ""

        win = self._window()
        try:
            win.cfg.transcription.speak_responses = True
            with patch("wizpr_suite.ui.main_window.speak_text", fake_speak):
                win._maybe_speak_response("first")
                win._maybe_speak_response("second")
                win.loop.run_until_complete(asyncio.sleep(0.05))

            self.assertEqual(["first second"], spoken)
            self.assertFalse(win._speech_queue)
        finally:
            win.close()

    def test_replay_last_response_speaks_previous_reply(self) -> None:
        spoken: list[str] = []

        async def fake_speak(text: str, voice: str = "", rate: int = 0) -> tuple[bool, str]:
            spoken.append(text)
            await asyncio.sleep(0)
            return True, ""

        win = self._window()
        try:
            win.speak_responses_check.setChecked(False)

            win._maybe_speak_response("last answer")

            self.assertEqual("last answer", win._last_response_text)
            self.assertTrue(win.replay_response_btn.isEnabled())
            self.assertEqual([], spoken)

            win.speak_responses_check.setChecked(True)
            with patch("wizpr_suite.ui.main_window.speak_text", fake_speak):
                win._replay_last_response()
                win.loop.run_until_complete(asyncio.sleep(0.05))

            self.assertEqual(["last answer"], spoken)
        finally:
            win.close()

    def test_replay_button_ignores_error_responses(self) -> None:
        win = self._window()
        try:
            win._maybe_speak_response("[Codex error] nope")

            self.assertEqual("", win._last_response_text)
            self.assertFalse(win.replay_response_btn.isEnabled())
        finally:
            win.close()

    def test_stop_speech_clears_queue_and_cancels_current_response(self) -> None:
        win = self._window()
        try:
            started = asyncio.Event()

            async def fake_speak(text: str, voice: str = "", rate: int = 0) -> tuple[bool, str]:
                started.set()
                await asyncio.sleep(30)
                return True, ""

            win.cfg.transcription.speak_responses = True
            with patch("wizpr_suite.ui.main_window.speak_text", fake_speak):
                win._maybe_speak_response("first")
                win.loop.run_until_complete(asyncio.wait_for(started.wait(), timeout=1.0))
                win._maybe_speak_response("second")

                win._stop_speech()
                win.loop.run_until_complete(asyncio.sleep(0))

            self.assertFalse(win._speech_queue)
            self.assertIsNone(win._speech_task)
        finally:
            win.close()

    def test_active_llm_choice_loads_and_saves(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(json.dumps({"active_llm_id": "ollama"}), encoding="utf-8")
            win = MainWindow(app_dir)
            try:
                self.assertEqual("ollama", win.active_llm_id)
                self.assertEqual("Ollama", win.active_llm_combo.currentText())
                self.assertEqual("ollama", win.active_llm_combo.currentData())

                idx = win.active_llm_combo.findData("openai_compat")
                self.assertGreaterEqual(idx, 0)
                win.active_llm_combo.setCurrentIndex(idx)
                self.assertEqual("openai_compat", load_config(app_dir).active_llm_id)
                self.assertEqual("Compatible Server", win.active_llm_combo.currentText())
            finally:
                win.close()

    def test_provider_pages_can_switch_active_talk_provider(self) -> None:
        win = self._window()
        try:
            app_dir = Path(self.tmp.name)

            win._advanced_changed(True)

            win.ollama_url.setText("http://127.0.0.1:11434")
            win.ollama_model.setCurrentText("qwen2.5-coder:14b")
            win.ollama_use_talk.click()

            cfg = load_config(app_dir)
            self.assertEqual("ollama", cfg.active_llm_id)
            self.assertEqual("ollama", win.active_llm_id)
            self.assertEqual("ollama", win.active_llm_combo.currentData())
            self.assertEqual("qwen2.5-coder:14b", cfg.ollama.model)

            win.compat_url.setText("http://127.0.0.1:8080")
            win.compat_model.setCurrentText("local-model")
            win.compat_use_talk.click()

            cfg = load_config(app_dir)
            self.assertEqual("openai_compat", cfg.active_llm_id)
            self.assertEqual("openai_compat", win.active_llm_combo.currentData())
            self.assertEqual("local-model", cfg.openai_compat.model)

            win.openai_model.setCurrentText("gpt-4.1-mini")
            win.openai_use_talk.click()

            cfg = load_config(app_dir)
            self.assertEqual("openai", cfg.active_llm_id)
            self.assertEqual("openai", win.active_llm_combo.currentData())
            self.assertEqual("gpt-4.1-mini", cfg.openai.model)
        finally:
            win.close()

    def test_assistant_output_uses_friendly_provider_label(self) -> None:
        class FakeProvider:
            async def generate(self, prompt: str, model: str, temperature: float = 0.7) -> LLMResponse:
                return LLMResponse("done")

        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(
                json.dumps({"active_llm_id": "openai_compat", "openai_compat": {"model": "local-model"}}),
                encoding="utf-8",
            )
            win = MainWindow(app_dir)
            try:
                win.registry._providers["openai_compat"] = FakeProvider()
                win.speak_responses_check.setChecked(False)

                win.loop.run_until_complete(win._send_prompt_to_assistant("hello"))

                text = win.output.toPlainText()
                self.assertIn("[Compatible Server: local-model", text)
                self.assertNotIn("[openai_compat:", text)
            finally:
                win.close()

    def test_provider_setup_button_opens_advanced_provider_for_active_llm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(json.dumps({"active_llm_id": "ollama"}), encoding="utf-8")
            win = MainWindow(app_dir)
            try:
                self.assertFalse(win._advanced_tabs_built)

                win._show_provider_settings()

                self.assertTrue(win._advanced_tabs_built)
                self.assertTrue(win.cfg.show_advanced_options)
                self.assertEqual("Providers", win.tabs.tabText(win.tabs.currentIndex()))
                self.assertEqual("Ollama", win.llm_tabs.tabText(win.llm_tabs.currentIndex()))
            finally:
                win.close()

    def test_simple_ollama_status_selects_qwen_model_without_advanced_tabs(self) -> None:
        class FakeOllama:
            def __init__(self) -> None:
                self.base_url = "http://127.0.0.1:11434"

            async def discover_base_url(self, preferred_base_url: str = "", timeout: float = 1.25) -> tuple[str, str]:
                self.base_url = "http://127.0.0.1:11434"
                return self.base_url, "models=2"

            async def list_models(self) -> tuple[list[str], str]:
                return ["llama3.1:8b", "qwen2.5-coder:14b"], ""

        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(
                json.dumps({"active_llm_id": "ollama", "ollama": {"model": "missing"}}),
                encoding="utf-8",
            )
            win = MainWindow(app_dir)
            try:
                win.p_ollama = FakeOllama()

                win.loop.run_until_complete(win._refresh_active_llm_status())

                self.assertEqual("qwen2.5-coder:14b", win.cfg.ollama.model)
                self.assertIn("qwen2.5-coder:14b", win.active_llm_detail.text())
                self.assertFalse(win._advanced_tabs_built)
            finally:
                win.close()

    def test_ollama_status_model_list_is_bounded(self) -> None:
        class SlowOllama:
            def __init__(self) -> None:
                self.base_url = "http://127.0.0.1:11434"

            async def discover_base_url(self, preferred_base_url: str = "", timeout: float = 1.25) -> tuple[str, str]:
                return self.base_url, "models=unknown"

            async def list_models(self) -> tuple[list[str], str]:
                await asyncio.sleep(30.0)
                return ["qwen2.5-coder:14b"], ""

        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(json.dumps({"active_llm_id": "ollama"}), encoding="utf-8")
            win = MainWindow(app_dir)
            try:
                win.p_ollama = SlowOllama()
                with patch.object(main_window, "OLLAMA_STATUS_MODEL_TIMEOUT_SECONDS", 0.05):
                    win.loop.run_until_complete(win._refresh_active_llm_status())

                self.assertIn("found at", win.active_llm_detail.text())
                self.assertFalse(win._advanced_tabs_built)
            finally:
                win.close()

    def test_saved_ring_scan_prefers_matching_ring_address(self) -> None:
        class FakeBle:
            async def scan_wizpr(self, seconds: float, include_reverse_ble: bool = False) -> list[DiscoveredDevice]:
                return [
                    DiscoveredDevice(address="AA:BB:CC:DD:EE:01", name="WIZPR RING-AA:01", rssi=-55),
                    DiscoveredDevice(address="AA:BB:CC:DD:EE:02", name="WIZPR RING-AA:02", rssi=-45),
                ]

            async def watch_windows_associated_devices(self, seconds: float) -> list[DiscoveredDevice]:
                return []

        win = self._window()
        try:
            win.ble = FakeBle()

            dev = win.loop.run_until_complete(win._scan_for_saved_ring("AA:BB:CC:DD:EE:01", timeout=1.0))

            self.assertIsNotNone(dev)
            self.assertEqual("AA:BB:CC:DD:EE:01", dev.address)
        finally:
            win.close()

    def test_remembered_ring_uses_sdk_setup_without_building_diagnostics(self) -> None:
        class FakeRing:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def connect(self, timeout: float = 18.0, lookup: bool = True, retry: bool = True) -> None:
                self.calls.append("connect")

            async def start_wizpr_session(self) -> None:
                self.calls.extend(["signature", "subscribe", "configure"])

        win = self._window()
        try:
            fake_ring = FakeRing()
            win.ring = fake_ring
            with (
                patch.object(win, "_start_ring_keepalive", lambda: None),
                patch.object(win, "_warm_transcriber_after_ring_connect", lambda: None),
                patch.object(win, "_play_feedback_sound", lambda name: None),
            ):
                ok = win.loop.run_until_complete(
                    win._connect_remembered_ring("AA:BB:CC:DD:EE:01", timeout=18.0, quick=True)
                )

            self.assertTrue(ok)
            self.assertEqual(["connect", "signature", "subscribe", "configure"], fake_ring.calls)
            self.assertFalse(win._advanced_ble_built)
        finally:
            win.close()

    def test_advanced_toggle_builds_provider_tabs(self) -> None:
        win = self._window()
        try:
            win._advanced_changed(True)
            tabs = [win.tabs.tabText(i) for i in range(win.tabs.count())]
            visible = {win.tabs.tabText(i): win.tabs.isTabVisible(i) for i in range(win.tabs.count())}

            self.assertIn("Providers", tabs)
            self.assertIn("Bridge", tabs)
            self.assertTrue(visible["Providers"])
            self.assertTrue(hasattr(win, "llm_tabs"))
            self.assertEqual("Voice", win.llm_tabs.tabText(win.llm_tabs.count() - 1))
            actions = [win.map_action.itemText(i) for i in range(win.map_action.count())]
            self.assertIn("copy_audio_to_clipboard", actions)
            self.assertIn("paste_audio_to_active_app", actions)
            self.assertIn("copy_last_transcript", actions)
            self.assertIn("paste_last_transcript", actions)
        finally:
            win.close()

    def test_advanced_toggle_builds_ring_diagnostics(self) -> None:
        win = self._window()
        try:
            win._append_ble_log("queued before advanced")
            win._advanced_changed(True)

            self.assertTrue(win._advanced_ble_built)
            self.assertTrue(hasattr(win, "ble_table"))
            self.assertTrue(hasattr(win, "gatt_tree"))
            self.assertTrue(hasattr(win, "notify_box"))
            self.assertEqual("Proximity Check", win.wizpr_proxy_btn.text())
            self.assertIn("GET_PROXY", win.wizpr_proxy_btn.toolTip())
            self.assertEqual("Proximity: --", win.wizpr_proximity_status.text())
            self.assertIn("queued before advanced", win.notify_box.toPlainText())
        finally:
            win.close()

    def test_proximity_check_uses_get_proxy_and_updates_status(self) -> None:
        class FakeRing:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def query_proxy(self) -> None:
                self.calls.append("query_proxy")

        win = self._window()
        try:
            win._advanced_changed(True)
            fake_ring = FakeRing()
            win.ring = fake_ring

            win._query_wizpr_proxy()
            win.loop.run_until_complete(asyncio.sleep(0.05))

            self.assertEqual(["query_proxy"], fake_ring.calls)
            self.assertEqual("Proximity check sent.", win.statusBar().currentMessage())

            handler = win.loop.run_until_complete(win._mk_notify_topic_handler("proxy"))
            win.loop.run_until_complete(handler({"text": "VIDLE=123"}))

            self.assertEqual("Proximity: VIDLE=123", win.wizpr_proximity_status.text())
            self.assertIn("Proximity check: VIDLE=123", win.ring_activity_box.toPlainText())
        finally:
            win.close()

    def test_advanced_scan_seconds_matches_visible_control(self) -> None:
        win = self._window()
        try:
            self.assertEqual(60.0, win._advanced_scan_seconds())

            win._advanced_changed(True)

            self.assertEqual(60.0, win._advanced_scan_seconds())
            win.ble_scan_seconds.setValue(12.0)
            self.assertEqual(12.0, win._advanced_scan_seconds())
        finally:
            win.close()

    def test_bridge_url_can_be_copied_from_advanced_tab(self) -> None:
        win = self._window()
        try:
            win._advanced_changed(True)
            self.assertEqual("http://127.0.0.1:8844/app", win.bridge_phone_url.text())

            win._copy_mobile_bridge_url()

            self.assertEqual("http://127.0.0.1:8844/app", QtWidgets.QApplication.clipboard().text())
        finally:
            win.close()

    def test_public_bridge_host_auto_generates_token_and_visible_phone_url(self) -> None:
        win = self._window()
        try:
            win._advanced_changed(True)
            win.bridge_host.setText("0.0.0.0")
            win.bridge_token.setText("")

            win._save_mobile_bridge()

            self.assertTrue(win.bridge_token.text())
            self.assertIn("/app?token=", win.bridge_phone_url.text())
            self.assertEqual(win.bridge_token.text(), load_config(Path(self.tmp.name)).mobile_bridge.token)
            self.assertIn("Bridge off", win.quick_ready_label.text())
        finally:
            win.close()

    def test_quick_ready_line_tracks_bridge_autostart_setting(self) -> None:
        win = self._window()
        try:
            win._advanced_changed(True)
            win.bridge_enabled.setChecked(True)
            win._save_mobile_bridge()

            self.assertIn("Bridge autostart", win.quick_ready_label.text())
        finally:
            win.close()

    def test_pending_bridge_request_surfaces_in_simple_status(self) -> None:
        win = self._window()
        try:
            self.assertFalse(win._advanced_tabs_built)

            win.loop.run_until_complete(
                win._on_mobile_bridge_request(
                    {
                        "id": "bridge-1",
                        "target": "assistant",
                        "source": "phone",
                        "text": "hello from phone",
                    }
                )
            )

            self.assertIn("Bridge 1 approval pending", win.quick_ready_label.text())
            self.assertIn("review the pending Bridge command", win.next_step_label.text())
            self.assertFalse(win._advanced_tabs_built)
            self.assertFalse(hasattr(win, "bridge_pending"))

            win._advanced_changed(True)

            self.assertEqual(1, win.bridge_pending.rowCount())

            win.bridge_pending.selectRow(0)
            win._reject_mobile_bridge_request()
            win.loop.run_until_complete(asyncio.sleep(0))

            self.assertEqual(0, win._bridge_pending_count())
            self.assertNotIn("approval pending", win.quick_ready_label.text())
        finally:
            win.close()

    def test_mobile_bridge_status_payload_reflects_simple_desktop_state(self) -> None:
        win = self._window()
        try:
            win.cfg.last_ble_address = "AA:BB:CC:DD:EE:01"
            win._set_ring_voice_target("codex")
            win._set_ring_connection_status("Connected", "connected")

            status = win._mobile_bridge_status_payload()

            self.assertIn("Ring connected", status["ready"])
            self.assertIn("raise the ring and speak", status["next_step"])
            self.assertTrue(status["ring"]["saved"])
            self.assertEqual("Connected", status["ring"]["status"])
            self.assertEqual("codex", status["voice"]["target"])
            self.assertTrue(status["voice"]["wake_required"])
            self.assertEqual("Codex", status["voice"]["wake_phrase"])
            self.assertEqual(win.active_llm_id, status["assistant"]["active_llm"])
        finally:
            win.close()

    def test_advanced_pages_do_not_need_horizontal_scrollbars_at_minimum_width(self) -> None:
        win = self._window()
        try:
            win._advanced_changed(True)
            win.resize(900, 620)
            win.show()
            self.app.processEvents()

            scrolls = win.findChildren(QtWidgets.QScrollArea)
            self.assertTrue(scrolls)
            for scroll in scrolls:
                self.assertEqual(QtCore.Qt.ScrollBarAlwaysOff, scroll.horizontalScrollBarPolicy())
        finally:
            win.close()

    def test_bridge_buttons_wrap_into_two_rows(self) -> None:
        win = self._window()
        try:
            win._advanced_changed(True)
            layout = win.bridge_save.parentWidget().layout()

            self.assertIsInstance(layout, QtWidgets.QGridLayout)
            self.assertEqual(2, layout.rowCount())
        finally:
            win.close()

    def test_provider_saves_do_not_need_advanced_widgets(self) -> None:
        win = self._window()
        try:
            win._save_openai()
            win._save_ollama()
            win._save_compat()
            win._save_codex()
            win._save_opencode()

            self.assertFalse(hasattr(win, "llm_tabs"))
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main()
