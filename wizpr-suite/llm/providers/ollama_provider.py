from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from collections.abc import AsyncIterator
from functools import lru_cache
from urllib.parse import urlsplit

import httpx

from ..base import LLMResponse

DEFAULT_OLLAMA_PORT = 11434
DEFAULT_OLLAMA_BASE_URL = f"http://127.0.0.1:{DEFAULT_OLLAMA_PORT}"
_HEALTH_CACHE_SECONDS = 15.0
_MODEL_CACHE_SECONDS = 5.0


class OllamaProvider:
    id = "ollama"
    display_name = "Ollama (local)"

    def __init__(self, base_url: str = DEFAULT_OLLAMA_BASE_URL) -> None:
        self._base_url = self.normalize_base_url(base_url) or DEFAULT_OLLAMA_BASE_URL
        self._client: httpx.AsyncClient | None = None
        self._healthy_until = 0.0
        self._models_cache: list[str] = []
        self._models_cached_at = 0.0

    @property
    def base_url(self) -> str:
        return self._base_url

    def configure(self, base_url: str) -> None:
        normalized = self.normalize_base_url(base_url) or DEFAULT_OLLAMA_BASE_URL
        if normalized == self._base_url:
            return
        self._base_url = normalized
        self._healthy_until = 0.0
        self._models_cache = []
        self._models_cached_at = 0.0

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=2.0),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5, keepalive_expiry=30.0),
                trust_env=False,
            )
        return self._client

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None and not client.is_closed:
            await client.aclose()

    async def is_healthy(self) -> tuple[bool, str]:
        ok, msg = await self._ensure_server()
        return (True, msg) if ok else (False, msg)

    async def list_models(self, force: bool = False) -> tuple[list[str], str]:
        now = time.monotonic()
        if not force and self._models_cache and now - self._models_cached_at < _MODEL_CACHE_SECONDS:
            return list(self._models_cache), ""
        try:
            ok, msg = await self._ensure_server()
            if not ok:
                return [], msg
            response = await self._get_client().get(self._url("/api/tags"), timeout=8.0)
            response.raise_for_status()
            data = response.json()
            models = sort_ollama_models(
                [str(item.get("name") or "").strip() for item in data.get("models", []) or []]
            )
            models = [model for model in models if model]
            self._models_cache = models
            self._models_cached_at = now
            self._healthy_until = now + _HEALTH_CACHE_SECONDS
            return list(models), ""
        except Exception as exc:
            return [], str(exc)

    async def warm_model(self, model: str) -> str:
        model = model.strip()
        if not model:
            return ""
        payload = {
            "model": model,
            "prompt": "",
            "stream": False,
            "keep_alive": self._keep_alive(),
            "options": {"num_predict": 0},
        }
        try:
            response = await self._get_client().post(self._url("/api/generate"), json=payload, timeout=120.0)
            response.raise_for_status()
            self._healthy_until = time.monotonic() + _HEALTH_CACHE_SECONDS
            return ""
        except Exception as exc:
            return str(exc)

    async def generate(self, prompt: str, model: str, temperature: float = 0.7) -> LLMResponse:
        try:
            model, error = await self._resolve_model(model)
            if error:
                return LLMResponse(text=f"[Ollama error] {error}", raw=None)
            payload = self._payload(prompt, model, temperature, stream=False)
            response = await self._post_with_discovery(payload, timeout=300.0)
            response.raise_for_status()
            data = response.json()
            self._healthy_until = time.monotonic() + _HEALTH_CACHE_SECONDS
            return LLMResponse(text=str(data.get("response") or ""), raw=data)
        except Exception as exc:
            return LLMResponse(text=f"[Ollama error] {exc}", raw=None)

    async def stream_generate(self, prompt: str, model: str, temperature: float = 0.7) -> AsyncIterator[str]:
        model, error = await self._resolve_model(model)
        if error:
            yield f"[Ollama error] {error}"
            return

        payload = self._payload(prompt, model, temperature, stream=True)
        attempted_discovery = False
        while True:
            yielded = False
            try:
                async with self._get_client().stream(
                    "POST",
                    self._url("/api/generate"),
                    json=payload,
                    timeout=httpx.Timeout(300.0, connect=2.0),
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        data = json.loads(line)
                        chunk = str(data.get("response") or "")
                        if chunk:
                            yielded = True
                            yield chunk
                        if data.get("done"):
                            self._healthy_until = time.monotonic() + _HEALTH_CACHE_SECONDS
                            return
                return
            except httpx.TransportError as exc:
                if yielded or attempted_discovery:
                    yield f"\n[Ollama error] {exc}"
                    return
                attempted_discovery = True
                found, message = await self.discover_base_url()
                if not found:
                    yield f"[Ollama error] {message or exc}"
                    return
            except Exception as exc:
                yield f"\n[Ollama error] {exc}" if yielded else f"[Ollama error] {exc}"
                return

    async def discover_base_url(self, preferred_base_url: str = "", timeout: float = 1.25) -> tuple[str, str]:
        candidates = self.candidate_base_urls(preferred_base_url or self._base_url)
        if not candidates:
            return "", "No Ollama server URL is configured."

        client = self._get_client()
        first = candidates[0]
        ok, message = await self._probe_base_url(client, first, timeout)
        if ok:
            self._mark_healthy(first)
            return first, message

        remaining = candidates[1:]
        results = await asyncio.gather(
            *(self._probe_base_url(client, url, timeout) for url in remaining),
            return_exceptions=True,
        )
        errors = [f"{first}: {message}"]
        for url, result in zip(remaining, results):
            if isinstance(result, Exception):
                errors.append(f"{url}: {result}")
                continue
            found, detail = result
            if found:
                self._mark_healthy(url)
                return url, detail
            errors.append(f"{url}: {detail}")

        preview = "; ".join(errors[:4])
        suffix = f" Tried: {preview}" if preview else ""
        return "", f"No Ollama server found on common URLs.{suffix}"

    async def _ensure_server(self) -> tuple[bool, str]:
        if time.monotonic() < self._healthy_until:
            return True, self._base_url
        ok, msg = await self._probe_current()
        if ok:
            self._healthy_until = time.monotonic() + _HEALTH_CACHE_SECONDS
            return True, f"{self._base_url} {msg}".strip()
        found, found_msg = await self.discover_base_url()
        if found:
            return True, f"{found} {found_msg}".strip()
        return False, found_msg or msg

    async def _probe_current(self) -> tuple[bool, str]:
        return await self._probe_base_url(self._get_client(), self._base_url, 2.0)

    @staticmethod
    async def _probe_base_url(client: httpx.AsyncClient, base_url: str, timeout: float) -> tuple[bool, str]:
        try:
            response = await client.get(base_url.rstrip("/") + "/api/tags", timeout=timeout)
            if response.status_code >= 400:
                return False, f"HTTP {response.status_code}"
            data = response.json()
            if not isinstance(data.get("models", []), list):
                return False, "responded, but not like Ollama /api/tags"
            return True, f"models={len(data.get('models') or [])}"
        except Exception as exc:
            return False, str(exc)

    async def _resolve_model(self, model: str) -> tuple[str, str]:
        model = model.strip()
        if model:
            return model, ""
        models, error = await self.list_models()
        if error:
            return "", error
        if not models:
            return "", "No Ollama model is installed or selected."
        return models[0], ""

    async def _post_with_discovery(self, payload: dict[str, object], timeout: float) -> httpx.Response:
        try:
            return await self._get_client().post(self._url("/api/generate"), json=payload, timeout=timeout)
        except httpx.TransportError as first_error:
            found, message = await self.discover_base_url()
            if not found:
                raise RuntimeError(message or str(first_error)) from first_error
            return await self._get_client().post(self._url("/api/generate"), json=payload, timeout=timeout)

    def _payload(self, prompt: str, model: str, temperature: float, stream: bool) -> dict[str, object]:
        return {
            "model": model,
            "prompt": prompt,
            "stream": stream,
            "keep_alive": self._keep_alive(),
            "options": {"temperature": float(temperature)},
        }

    @staticmethod
    def _keep_alive() -> str:
        return os.environ.get("WIZPR_OLLAMA_KEEP_ALIVE", "30m").strip() or "30m"

    def _url(self, path: str) -> str:
        return self._base_url.rstrip("/") + path

    def _mark_healthy(self, base_url: str) -> None:
        self._base_url = base_url
        self._healthy_until = time.monotonic() + _HEALTH_CACHE_SECONDS

    @classmethod
    def candidate_base_urls(cls, preferred_base_url: str = "") -> list[str]:
        out: list[str] = []

        def add(value: str) -> None:
            for url in cls._expand_bind_base_url(value):
                if url and url not in out:
                    out.append(url)

        add(preferred_base_url)
        add(os.environ.get("OLLAMA_HOST", ""))
        add(DEFAULT_OLLAMA_BASE_URL)
        add(f"http://localhost:{DEFAULT_OLLAMA_PORT}")
        add(f"http://[::1]:{DEFAULT_OLLAMA_PORT}")
        add(f"http://host.docker.internal:{DEFAULT_OLLAMA_PORT}")

        for host in cls._local_host_candidates():
            add(f"http://{host}:{DEFAULT_OLLAMA_PORT}")

        return out

    @classmethod
    def _expand_bind_base_url(cls, value: str) -> list[str]:
        normalized = cls.normalize_base_url(value)
        if not normalized:
            return []
        try:
            parsed = urlsplit(normalized)
            host = parsed.hostname or ""
            port = parsed.port or DEFAULT_OLLAMA_PORT
        except Exception:
            return [normalized]

        if host in {"0.0.0.0", "::"}:
            return [
                f"http://127.0.0.1:{port}",
                f"http://localhost:{port}",
                f"http://[::1]:{port}",
                *[f"http://{candidate}:{port}" for candidate in cls._local_host_candidates()],
            ]
        return [normalized]

    @staticmethod
    def normalize_base_url(value: str) -> str:
        raw = (value or "").strip().strip('"').strip("'")
        if not raw:
            return ""
        if raw.startswith(":"):
            raw = f"127.0.0.1{raw}"
        if "://" not in raw:
            raw = f"http://{raw}"
        try:
            parsed = urlsplit(raw)
            scheme = parsed.scheme or "http"
            host = parsed.hostname
            if not host:
                return raw.rstrip("/")
            if host in {"0.0.0.0", "::"}:
                host = "127.0.0.1"
            port = parsed.port or DEFAULT_OLLAMA_PORT
            host_part = f"[{host}]" if ":" in host and not host.startswith("[") else host
            return f"{scheme}://{host_part}:{port}"
        except Exception:
            return raw.rstrip("/")

    @staticmethod
    @lru_cache(maxsize=1)
    def _local_host_candidates() -> tuple[str, ...]:
        out: list[str] = []

        def add(value: str) -> None:
            value = (value or "").strip()
            if value and value not in out:
                out.append(value)

        add(socket.gethostname())
        add(socket.getfqdn())

        for host in list(out):
            try:
                infos = socket.getaddrinfo(host, None)
            except Exception:
                continue
            for family, _type, _proto, _canon, sockaddr in infos:
                if family not in (socket.AF_INET, socket.AF_INET6):
                    continue
                addr = sockaddr[0]
                if addr.startswith("127.") or addr.startswith("169.254.") or addr == "::1" or addr.startswith("fe80:") or "%" in addr:
                    continue
                add(addr)

        return tuple(out)


def sort_ollama_models(models: list[str]) -> list[str]:
    return sorted(set(models), key=_ollama_model_sort_key)


def _ollama_model_sort_key(model: str) -> tuple[int, str]:
    text = model.casefold()
    score = 50
    if "qwen" in text:
        score -= 20
    if "coder" in text or "code" in text:
        score -= 8
    if "qwen36-27b" in text or "qwen3" in text:
        score -= 6
    if "llama" in text:
        score -= 2
    return score, text
