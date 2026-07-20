from __future__ import annotations

import unittest
from pathlib import Path

from wizpr_suite.core.speech import text_for_speech


class SpeechTests(unittest.TestCase):
    def test_text_for_speech_removes_code_blocks_and_links(self) -> None:
        text = """
        Here is the answer.

        ```python
        print("do not read this")
        ```

        More at https://example.com/path.
        """

        spoken = text_for_speech(text)

        self.assertIn("Here is the answer.", spoken)
        self.assertIn("code omitted", spoken)
        self.assertIn("link", spoken)
        self.assertNotIn("print", spoken)
        self.assertNotIn("https://", spoken)

    def test_text_for_speech_skips_ui_logs_tables_and_tracebacks(self) -> None:
        text = """
        > [codex] Open Notepad.
        [transcript] ignored transcript text

        | file | status |
        | ---- | ------ |
        | app.py | changed |

        The useful answer is here.

        Traceback (most recent call last):
          File "bad.py", line 1, in <module>
        RuntimeError: noisy details
        """

        spoken = text_for_speech(text)

        self.assertIn("The useful answer is here.", spoken)
        self.assertIn("error details omitted", spoken)
        self.assertNotIn("Open Notepad", spoken)
        self.assertNotIn("ignored transcript", spoken)
        self.assertNotIn("app.py", spoken)
        self.assertNotIn("RuntimeError", spoken)

    def test_text_for_speech_ignores_empty_text(self) -> None:
        self.assertEqual("", text_for_speech("   "))


if __name__ == "__main__":
    unittest.main()


def test_speech_does_not_create_or_open_response_text_files() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "wizpr_suite" / "core" / "speech.py").read_text(encoding="utf-8")
    lowered = source.casefold()
    assert "tempfile" not in lowered
    assert "mkstemp" not in lowered
    assert "notepad" not in lowered
    assert "[console]::in.readtoend()" in lowered
    assert 'stdin=asyncio.subprocess.pipe' in lowered
    assert 'creationflags=0x08000000' in lowered
