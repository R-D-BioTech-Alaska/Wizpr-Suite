from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from wizpr_suite.llm.providers.ollama_provider import OllamaProvider
from wizpr_suite.llm.providers.openai_compat_provider import OpenAICompatProvider
from wizpr_suite.llm.providers.openai_provider import OpenAIProvider


class ProviderStreamingTests(unittest.TestCase):
    def test_ollama_generation_skips_health_probe_and_reuses_client(self) -> None:
        requests: list[tuple[str, str]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            payload = json.loads(request.content)
            self.assertFalse(payload["stream"])
            self.assertEqual("30m", payload["keep_alive"])
            return httpx.Response(200, json={"response": "ready"})

        async def run() -> None:
            provider = OllamaProvider()
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            provider._client = client
            try:
                response = await provider.generate("hello", "model")
                self.assertEqual("ready", response.text)
                self.assertIs(client, provider._get_client())
            finally:
                await provider.close()

        asyncio.run(run())
        self.assertEqual([("POST", "/api/generate")], requests)

    def test_ollama_stream_returns_chunks_as_they_arrive(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            body = "\n".join(
                [
                    json.dumps({"response": "fast", "done": False}),
                    json.dumps({"response": " answer", "done": False}),
                    json.dumps({"response": "", "done": True}),
                ]
            )
            return httpx.Response(200, text=body + "\n")

        async def run() -> None:
            provider = OllamaProvider()
            provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                chunks = [chunk async for chunk in provider.stream_generate("hello", "model")]
                self.assertEqual(["fast", " answer"], chunks)
            finally:
                await provider.close()

        asyncio.run(run())

    def test_compat_stream_parses_server_sent_events(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            body = (
                'data: {"choices":[{"delta":{"content":"local"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":" reply"}}]}\n\n'
                "data: [DONE]\n\n"
            )
            return httpx.Response(200, text=body)

        async def run() -> None:
            provider = OpenAICompatProvider()
            provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                chunks = [chunk async for chunk in provider.stream_generate("hello", "model")]
                self.assertEqual(["local", " reply"], chunks)
            finally:
                await provider.close()

        asyncio.run(run())

    def test_openai_stream_uses_async_client(self) -> None:
        async def events():
            for text in ("cloud", " reply"):
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=text))]
                )

        class FakeCompletions:
            async def create(self, **kwargs: object) -> object:
                self.kwargs = kwargs
                return events()

        completions = FakeCompletions()
        provider = OpenAIProvider(api_key="key")
        provider._async_client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )

        async def run() -> None:
            chunks = [chunk async for chunk in provider.stream_generate("hello", "model")]
            self.assertEqual(["cloud", " reply"], chunks)

        asyncio.run(run())
        self.assertTrue(completions.kwargs["stream"])

    def test_openai_transcription_still_accepts_injected_sync_client(self) -> None:
        calls: list[dict[str, object]] = []

        class FakeTranscriptions:
            def create(self, **kwargs: object) -> object:
                calls.append(dict(kwargs))
                return SimpleNamespace(text="ready")

        provider = OpenAIProvider(api_key="key")
        provider._client = SimpleNamespace(audio=SimpleNamespace(transcriptions=FakeTranscriptions()))
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "audio.wav"
            audio.write_bytes(b"audio")
            text, error = asyncio.run(provider.transcribe_audio(audio))
        self.assertEqual("ready", text)
        self.assertEqual("", error)
        self.assertEqual(0.0, calls[0]["temperature"])


if __name__ == "__main__":
    unittest.main()
