from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from wizpr_suite.llm.providers.openai_provider import OpenAIProvider


class OpenAIProviderTests(unittest.TestCase):
    def test_transcription_uses_vocabulary_prompt_and_zero_temperature(self) -> None:
        calls: list[dict[str, object]] = []

        class FakeTranscriptions:
            def create(self, **kwargs: object) -> object:
                calls.append(dict(kwargs))
                return SimpleNamespace(text="Codex, check Ollama")

        provider = OpenAIProvider(api_key="test")
        provider._client = SimpleNamespace(
            audio=SimpleNamespace(transcriptions=FakeTranscriptions())
        )

        with tempfile.TemporaryDirectory() as td:
            audio = Path(td) / "voice.wav"
            audio.write_bytes(b"fake")

            text, err = asyncio.run(
                provider.transcribe_audio(
                    audio,
                    model="gpt-4o-mini-transcribe",
                    prompt=OpenAIProvider.transcription_prompt,
                )
            )

        self.assertEqual("Codex, check Ollama", text)
        self.assertEqual("", err)
        self.assertEqual("gpt-4o-mini-transcribe", calls[0]["model"])
        self.assertEqual(0.0, calls[0]["temperature"])
        self.assertEqual("en", calls[0]["language"])
        self.assertIn("Codex", calls[0]["prompt"])
        self.assertIn("Ollama", calls[0]["prompt"])
        self.assertEqual("voice.wav", calls[0]["file"][0])
        self.assertEqual(b"fake", calls[0]["file"][1])


if __name__ == "__main__":
    unittest.main()
