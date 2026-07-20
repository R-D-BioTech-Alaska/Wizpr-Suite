from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wizpr_suite.core.config import CONFIG_FILE, load_config


class ConfigTests(unittest.TestCase):
    def test_transcription_defaults_follow_wizpr_app_basics(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = load_config(Path(td))

            self.assertEqual("proximity", cfg.transcription.voice_mode)
            self.assertEqual("auto", cfg.transcription.stt_backend)
            self.assertEqual(5, cfg.transcription.ring_sleep_timeout_seconds)
            self.assertTrue(cfg.transcription.speak_responses)
            self.assertTrue(cfg.transcription.ring_connection_sound)
            self.assertFalse(cfg.transcription.mic_activation_sound)
            self.assertTrue(cfg.transcription.low_battery_warning)
            self.assertEqual("Wizpr", cfg.transcription.clipboard_wake_word)
            self.assertEqual("Wizpr", cfg.transcription.paste_wake_word)
            self.assertEqual(180, cfg.transcription.ring_audio_finalize_delay_ms)
            self.assertTrue(cfg.auto_connect_saved_ring)
            self.assertEqual("app", cfg.button_mode)
            self.assertNotIn("button_single", cfg.mappings["toggle_ring_lock"])
            self.assertTrue(cfg.protect_connected_ring_buttons)
            self.assertIn("button_double", cfg.mappings["start_new_chat"])
            self.assertIn("button_triple", cfg.mappings["edit_last_transcript"])
            self.assertNotIn("button_single", cfg.mappings["toggle_listen"])
            self.assertEqual("word", cfg.transcription.interrupt_mode)
            self.assertEqual("stop", cfg.transcription.interrupt_word)
            self.assertTrue(cfg.memory.enabled)
            self.assertEqual("ask", cfg.tools.permission_mode)


    def test_old_app_lock_mapping_is_removed_during_migration(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(
                json.dumps(
                    {
                        "button_mode": "app",
                        "mappings": {
                            "toggle_ring_lock": ["button_single"],
                            "start_new_chat": ["button_double"],
                            "edit_last_transcript": ["button_triple"],
                        },
                    }
                ),
                encoding="utf-8",
            )

            cfg = load_config(app_dir)

            self.assertEqual("app", cfg.button_mode)
            self.assertNotIn("button_single", cfg.mappings["toggle_ring_lock"])
            self.assertTrue(cfg.protect_connected_ring_buttons)

    def test_old_coding_button_mapping_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(
                json.dumps(
                    {
                        "mappings": {
                            "toggle_listen": ["button_single"],
                            "send_last_transcript": ["button_double"],
                            "send_last_to_codex": ["button_triple"],
                            "cycle_llm": ["button_long"],
                        }
                    }
                ),
                encoding="utf-8",
            )

            cfg = load_config(app_dir)

            self.assertEqual("coding", cfg.button_mode)
            self.assertIn("button_single", cfg.mappings["toggle_listen"])
            self.assertIn("button_double", cfg.mappings["send_last_transcript"])
            self.assertIn("button_triple", cfg.mappings["send_last_to_codex"])
            self.assertNotIn("button_single", cfg.mappings["toggle_ring_lock"])

    def test_custom_button_mapping_stays_custom(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(
                json.dumps({"mappings": {"copy_last_transcript": ["button_single"]}}),
                encoding="utf-8",
            )

            cfg = load_config(app_dir)

            self.assertEqual("custom", cfg.button_mode)
            self.assertIn("button_single", cfg.mappings["copy_last_transcript"])
            self.assertNotIn("button_single", cfg.mappings["toggle_ring_lock"])

    def test_load_config_ignores_unknown_nested_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(
                json.dumps(
                    {
                        "transcription": {
                            "local_model": "medium.en",
                            "unknown_future_setting": True,
                        }
                    }
                ),
                encoding="utf-8",
            )

            cfg = load_config(app_dir)

            self.assertEqual("medium.en", cfg.transcription.local_model)
            self.assertEqual("int8", cfg.transcription.local_compute_type)
            self.assertTrue(cfg.transcription.audio_preflight_enabled)
            self.assertTrue(cfg.transcription.hold_coding_voice_commands)
            self.assertIn("copy_audio_to_clipboard", cfg.mappings)
            self.assertIn("paste_audio_to_active_app", cfg.mappings)
            self.assertIn("copy_last_transcript", cfg.mappings)
            self.assertIn("paste_last_transcript", cfg.mappings)

    def test_old_voice_pipeline_migrates_assistant_wake_to_direct_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(
                json.dumps({"transcription": {"require_wake_word": True}}),
                encoding="utf-8",
            )

            cfg = load_config(app_dir)

            self.assertEqual(8, cfg.transcription.voice_pipeline_version)
            self.assertFalse(cfg.transcription.require_wake_word)
            self.assertEqual(0.18, cfg.transcription.audio_preflight_min_seconds)
            self.assertEqual(0.0012, cfg.transcription.audio_preflight_min_rms)
            self.assertEqual(0.12, cfg.transcription.audio_preflight_min_active_seconds)
            self.assertEqual(650, cfg.transcription.ring_audio_idle_finalize_delay_ms)


    def test_voice_pipeline_upgrade_uses_high_accuracy_openai_transcription(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(
                json.dumps(
                    {
                        "openai": {"transcription_model": "gpt-4o-mini-transcribe"},
                        "transcription": {"voice_pipeline_version": 4},
                    }
                ),
                encoding="utf-8",
            )

            cfg = load_config(app_dir)

            self.assertEqual(8, cfg.transcription.voice_pipeline_version)
            self.assertEqual("gpt-4o-transcribe", cfg.openai.transcription_model)

    def test_old_voice_stop_grace_defaults_migrate_to_faster_value(self) -> None:
        for old_delay in (250, 350, 500):
            with tempfile.TemporaryDirectory() as td:
                app_dir = Path(td)
                (app_dir / CONFIG_FILE).write_text(
                    json.dumps({"transcription": {"ring_audio_finalize_delay_ms": old_delay}}),
                    encoding="utf-8",
                )

                cfg = load_config(app_dir)

                self.assertEqual(180, cfg.transcription.ring_audio_finalize_delay_ms)

    def test_local_model_and_compute_choices_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(
                json.dumps({"transcription": {"local_model": "medium.en", "local_compute_type": "float32"}}),
                encoding="utf-8",
            )

            cfg = load_config(app_dir)

            self.assertEqual("medium.en", cfg.transcription.local_model)
            self.assertEqual("float32", cfg.transcription.local_compute_type)

    def test_active_llm_id_is_loaded_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(
                json.dumps({"active_llm_id": "ollama"}),
                encoding="utf-8",
            )

            cfg = load_config(app_dir)

            self.assertEqual("ollama", cfg.active_llm_id)

            (app_dir / CONFIG_FILE).write_text(
                json.dumps({"active_llm_id": "missing"}),
                encoding="utf-8",
            )

            cfg = load_config(app_dir)

            self.assertEqual("openai", cfg.active_llm_id)

    def test_ring_voice_target_can_be_clipboard_or_paste(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(json.dumps({"ring_voice_target": "paste"}), encoding="utf-8")

            cfg = load_config(app_dir)

            self.assertEqual("paste", cfg.ring_voice_target)
            self.assertIn("audio_capture", cfg.mappings["paste_audio_to_active_app"])
            self.assertNotIn("audio_capture", cfg.mappings["send_audio_to_assistant"])

    def test_old_audio_capture_paste_mapping_migrates_to_current_audio_paste(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            (app_dir / CONFIG_FILE).write_text(
                json.dumps({"mappings": {"paste_last_transcript": ["audio_capture"]}}),
                encoding="utf-8",
            )

            cfg = load_config(app_dir)

            self.assertEqual("paste", cfg.ring_voice_target)
            self.assertIn("audio_capture", cfg.mappings["paste_audio_to_active_app"])
            self.assertNotIn("audio_capture", cfg.mappings["paste_last_transcript"])

    def test_legacy_interrupt_phrase_migrates_to_stop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            payload = {
                "transcription": {
                    "voice_pipeline_version": 6,
                    "interrupt_mode": "word",
                    "interrupt_word": "Wizpr stop, Stop Wizpr",
                }
            }
            (app_dir / CONFIG_FILE).write_text(json.dumps(payload), encoding="utf-8")
            cfg = load_config(app_dir)
            self.assertEqual(8, cfg.transcription.voice_pipeline_version)
            self.assertEqual("stop", cfg.transcription.interrupt_word)



if __name__ == "__main__":
    unittest.main()
