from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import warnings
import wave
from pathlib import Path
from typing import Any

_MODELS: dict[tuple[str, str], object] = {}
_PERSISTENT_WORKER: _PersistentTranscriptionWorker | None = None
_PROMPT_LEAKS = {
    "short spoken command to a computer assistant",
    "words may include codex opencode ollama files code app ring",
    "ollama qwen clipboard paste assistant ring files code app",
    "qwen clipboard paste assistant ring files code app",
}
_NON_COMMAND_PHRASES = {
    "blank audio",
    "ticking sounds",
    "ticking sound",
    "clicking sounds",
    "clicking sound",
    "keyboard clicking",
    "typing sounds",
    "background noise",
    "static noise",
    "music",
    "silence",
    "silent",
    "no speech",
    "no speech detected",
    "inaudible",
    "noise",
}
_FILLER_ONLY_WORDS = {
    "ah",
    "eh",
    "hm",
    "hmm",
    "mm",
    "mmm",
    "oh",
    "uh",
    "um",
    "you",
}
_BOILERPLATE_TRANSCRIPTS = {
    "please subscribe to my channel",
    "subscribe to my channel",
    "subtitles by amara org",
    "subs by amara org",
    "thanks for watching",
    "thank you for watching",
}
_BOILERPLATE_PATTERNS = (
    re.compile(r"^(?:subs?|subtitles?|captions?)\s+by\b"),
    re.compile(r"^(?:subs?|subtitles?|captions?)\s+(?:provided|created|made)\s+by\b"),
    re.compile(r"\bwww\s+[a-z0-9-]+(?:\s+(?:com|co|org|net|uk|us|tv|io)){1,3}\b"),
    re.compile(r"\b(?:amara|zeoranger)\s+(?:org|co\s+uk)\b"),
)
_NON_COMMAND_STARTS = (
    "audio contains",
    "background audio",
    "caption",
    "description",
    "i hear",
    "sound of",
    "sounds of",
    "subtitles by",
    "there is no speech",
    "this audio",
    "transcription",
)


def _default_model_name() -> str:
    return os.environ.get("WIZPR_LOCAL_WHISPER_MODEL", "small.en").strip() or "small.en"


def _default_compute_type() -> str:
    return os.environ.get("WIZPR_LOCAL_WHISPER_COMPUTE", "int8").strip() or "int8"


def _env_timeout_seconds(name: str, default: str, minimum: float = 5.0) -> float:
    raw = os.environ.get(name, default).strip()
    try:
        return max(minimum, float(raw))
    except ValueError:
        try:
            return max(minimum, float(default))
        except ValueError:
            return minimum


def _default_timeout_seconds() -> float:
    return _env_timeout_seconds("WIZPR_LOCAL_WHISPER_TIMEOUT", "90")


def _default_request_timeout_seconds() -> float:
    default = os.environ.get("WIZPR_LOCAL_WHISPER_TIMEOUT", "12")
    return _env_timeout_seconds("WIZPR_LOCAL_WHISPER_REQUEST_TIMEOUT", default, minimum=3.0)


def local_transcription_request_timeout_seconds() -> float:
    return _default_request_timeout_seconds()


def local_transcription_uses_persistent_worker() -> bool:
    backend = os.environ.get("WIZPR_LOCAL_WHISPER_BACKEND", "server").strip().lower()
    return backend in {"server", "persistent", "warm"}


def _transcription_worker_command(*args: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--local-transcribe-worker", *args]
    return [sys.executable, "-m", "wizpr_suite.tools.local_transcribe_worker", *args]


def _default_beam_size() -> int:
    raw = os.environ.get("WIZPR_LOCAL_WHISPER_BEAM", "2").strip()
    try:
        return max(1, min(5, int(raw)))
    except ValueError:
        return 2


def _retry_beam_size() -> int:
    raw = os.environ.get("WIZPR_LOCAL_WHISPER_RETRY_BEAM", "5").strip()
    try:
        return max(2, min(5, int(raw)))
    except ValueError:
        return 5


def _default_hotwords() -> str:
    raw = os.environ.get("WIZPR_LOCAL_WHISPER_HOTWORDS")
    if raw is not None:
        return " ".join(raw.split())
    return "WIZPR Wizpr Codex OpenCode Ollama Qwen"


def _read_wav_float32(audio_path: Path) -> tuple[Any, int] | None:
    try:
        import numpy as np
    except Exception:
        return None

    try:
        with wave.open(str(audio_path), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frame_count = wav.getnframes()
            raw = wav.readframes(frame_count)
    except Exception:
        return None

    if not raw or frame_count <= 0:
        return None

    if sample_width == 1:
        audio = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sample_width == 2:
        audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 4:
        audio = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        return None

    if channels > 1:
        trim = (audio.size // channels) * channels
        if trim <= 0:
            return None
        audio = audio[:trim].reshape(-1, channels).mean(axis=1)

    if audio.size == 0:
        return None
    return audio.astype(np.float32), int(sample_rate or 16000)


def _prepare_audio_input(audio_path: Path) -> object:
    loaded = _read_wav_float32(audio_path)
    if loaded is None:
        return str(audio_path)
    try:
        import numpy as np
    except Exception:
        return str(audio_path)
    audio, sample_rate = loaded

    audio = audio - float(np.mean(audio))
    if sample_rate and sample_rate != 16000 and audio.size > 1:
        duration = audio.size / float(sample_rate)
        new_size = max(1, int(round(duration * 16000)))
        old_x = np.linspace(0.0, duration, num=audio.size, endpoint=False)
        new_x = np.linspace(0.0, duration, num=new_size, endpoint=False)
        audio = np.interp(new_x, old_x, audio).astype(np.float32)

    audio = _select_speech_region(audio.astype(np.float32), 16000)
    if audio.size == 0:
        return audio

    frame = max(1, int(16000 * 0.03))
    hop = max(1, int(16000 * 0.01))
    frame_rms = _frame_rms(audio, frame, hop)
    noise_floor = float(np.percentile(frame_rms, 20)) if frame_rms.size else 0.0
    if noise_floor > 0.0:
        gate = max(0.0007, noise_floor * 1.7)
        mask = np.abs(audio) < gate
        audio = audio.copy()
        audio[mask] *= 0.22

    abs_audio = np.abs(audio)
    reference = float(np.percentile(abs_audio, 92)) if abs_audio.size else 0.0
    if reference > 0.0005:
        gain = min(8.0, max(0.75, 0.34 / reference))
        audio = audio * gain

    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 0.98:
        audio = audio * (0.98 / peak)
    return np.clip(audio, -0.98, 0.98).astype(np.float32)


def _frame_rms(audio: Any, frame: int, hop: int) -> Any:
    import numpy as np

    if audio.size < frame:
        return np.empty(0, dtype=np.float32)
    starts = np.arange(0, audio.size - frame + 1, hop, dtype=np.int64)
    squared = np.square(audio, dtype=np.float64)
    cumulative = np.empty(squared.size + 1, dtype=np.float64)
    cumulative[0] = 0.0
    np.cumsum(squared, out=cumulative[1:])
    means = (cumulative[starts + frame] - cumulative[starts]) / float(frame)
    return np.sqrt(means).astype(np.float32)


def _longest_true_run(mask: Any) -> int:
    import numpy as np

    values = np.asarray(mask, dtype=bool)
    if values.size == 0 or not bool(values.any()):
        return 0
    false_positions = np.flatnonzero(~values)
    boundaries = np.concatenate((np.array([-1]), false_positions, np.array([values.size])))
    return int(np.max(np.diff(boundaries) - 1))


def audio_preflight_reason(
    audio_path: Path,
    *,
    min_seconds: float = 0.35,
    min_rms: float = 0.0025,
    min_active_seconds: float = 0.0,
) -> tuple[str, dict[str, float]]:
    loaded = _read_wav_float32(audio_path)
    if loaded is None:
        return "Could not read captured audio.", {}

    try:
        import numpy as np
    except Exception:
        return "", {}

    audio, sample_rate = loaded
    if audio.size == 0 or sample_rate <= 0:
        return "No captured audio samples.", {}

    audio = audio - float(np.mean(audio))
    duration = audio.size / float(sample_rate)
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
    metrics = {
        "duration_seconds": duration,
        "rms": rms,
        "peak": peak,
        "active_seconds": 0.0,
        "active_run_seconds": 0.0,
        "active_ratio": 0.0,
    }

    if duration < max(0.05, float(min_seconds)):
        return f"Ignored very short audio capture ({duration:.2f}s).", metrics
    if rms < max(0.0001, float(min_rms)) or peak < max(0.003, float(min_rms) * 2.0):
        return f"Ignored quiet capture (rms {rms:.4f}).", metrics

    frame = max(1, int(sample_rate * 0.03))
    hop = max(1, int(sample_rate * 0.01))
    if audio.size <= frame:
        active_seconds = duration if rms >= min_rms else 0.0
        metrics["active_run_seconds"] = active_seconds
        metrics["active_ratio"] = 1.0 if active_seconds else 0.0
    else:
        frame_rms = _frame_rms(audio, frame, hop)
        if frame_rms.size == 0:
            active_seconds = 0.0
        else:
            noise_floor = float(np.percentile(frame_rms, 20))
            active_threshold = max(float(min_rms) * 1.8, min(noise_floor * 3.0, rms * 0.75), rms * 0.35)
            active_mask = frame_rms >= active_threshold
            active_frames = int(np.count_nonzero(active_mask))
            active_seconds = active_frames * hop / float(sample_rate)
            metrics["active_ratio"] = active_frames / float(frame_rms.size)
            metrics["active_run_seconds"] = _longest_true_run(active_mask) * hop / float(sample_rate)

    metrics["active_seconds"] = active_seconds
    min_active = max(0.0, float(min_active_seconds))
    if min_active > 0.0:
        if active_seconds < max(0.05, min_active):
            return f"Ignored capture with too little speech-like audio ({active_seconds:.2f}s active).", metrics
        if metrics["active_run_seconds"] < max(0.08, min_active * 0.65):
            return (
                f"Ignored capture with no continuous speech-like audio "
                f"({metrics['active_run_seconds']:.2f}s longest run).",
                metrics,
            )
    return "", metrics


def _speech_regions(audio: Any, sample_rate: int) -> list[tuple[int, int]]:
    try:
        import numpy as np
    except Exception:
        return []
    if audio.size < max(1, int(sample_rate * 0.08)):
        return [(0, int(audio.size))] if audio.size else []

    frame = max(1, int(sample_rate * 0.03))
    hop = max(1, int(sample_rate * 0.01))
    rms = _frame_rms(audio, frame, hop)
    if rms.size == 0:
        return [(0, int(audio.size))]

    noise_floor = float(np.percentile(rms, 18))
    upper = float(np.percentile(rms, 90))
    threshold = max(0.0012, noise_floor * 2.4, upper * 0.11)
    active = rms >= threshold
    if not bool(active.any()):
        return []

    bridge = max(1, int(round(0.18 / (hop / float(sample_rate)))))
    if bridge > 1:
        kernel = np.ones(bridge, dtype=np.int16)
        active = np.convolve(active.astype(np.int16), kernel, mode="same") > 0

    positions = np.flatnonzero(active)
    regions: list[tuple[int, int]] = []
    start_frame = int(positions[0])
    previous = int(positions[0])
    split_frames = max(1, int(round(0.75 / (hop / float(sample_rate)))))
    for position in positions[1:]:
        current = int(position)
        if current - previous > split_frames:
            regions.append((start_frame * hop, min(audio.size, previous * hop + frame)))
            start_frame = current
        previous = current
    regions.append((start_frame * hop, min(audio.size, previous * hop + frame)))

    min_samples = int(sample_rate * 0.10)
    return [(start, end) for start, end in regions if end - start >= min_samples]


def _select_speech_region(audio: Any, sample_rate: int) -> Any:
    try:
        import numpy as np
    except Exception:
        return audio
    regions = _speech_regions(audio, sample_rate)
    if not regions:
        return audio

    pad_before = int(sample_rate * 0.12)
    pad_after = int(sample_rate * 0.18)
    join_silence = np.zeros(max(1, int(sample_rate * 0.10)), dtype=np.float32)
    selected: list[Any] = []
    last_end = -1
    for region_start, region_end in regions:
        start = max(0, region_start - pad_before)
        end = min(audio.size, region_end + pad_after)
        if last_end >= 0:
            start = max(start, last_end)
        if end <= start:
            continue
        if selected:
            selected.append(join_silence)
        selected.append(audio[start:end].astype(np.float32, copy=False))
        last_end = end
    if not selected:
        return audio
    return np.concatenate(selected).astype(np.float32, copy=False)


def _trim_silence(audio: Any, sample_rate: int) -> Any:
    return _select_speech_region(audio, sample_rate)


def _clean_transcript(text: str) -> str:
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return ""

    cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)
    cleaned = re.sub(r"([,.!?;:]){2,}", r"\1", cleaned)
    cleaned = re.sub(r",\s*\.", ".", cleaned)

    tokens = cleaned.split()
    collapsed: list[str] = []
    previous_word = ""
    repeat_count = 0
    for token in tokens:
        word = re.sub(r"[^A-Za-z0-9']+", "", token).casefold()
        if word and word == previous_word:
            repeat_count += 1
            if repeat_count >= 1:
                punctuation = re.search(r"([,.!?;:]+)$", token)
                if punctuation and collapsed and not re.search(r"[,.!?;:]$", collapsed[-1]):
                    collapsed[-1] = f"{collapsed[-1]}{punctuation.group(1)[0]}"
                continue
        else:
            previous_word = word
            repeat_count = 0
        collapsed.append(token)

    cleaned = " ".join(collapsed).strip()
    cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)
    cleaned = re.sub(r"[-\u2013\u2014]\s*$", "", cleaned).strip()
    return cleaned


def _reject_transcript_reason(text: str) -> str:
    norm = re.sub(r"[^a-z0-9\s']+", " ", text.casefold())
    norm = " ".join(norm.split())
    if not norm:
        return "No clear speech detected."
    if any(leak in norm for leak in _PROMPT_LEAKS) or "words may include" in norm:
        return "Ignored likely Whisper prompt hallucination."
    if norm in _NON_COMMAND_PHRASES:
        return "Ignored non-speech audio description."
    if norm.startswith(_NON_COMMAND_STARTS):
        return "Ignored non-speech audio description."
    if norm in _BOILERPLATE_TRANSCRIPTS or norm.startswith("thanks for watching"):
        return "Ignored likely transcription boilerplate."
    if any(pattern.search(norm) for pattern in _BOILERPLATE_PATTERNS):
        return "Ignored likely subtitle or website transcription hallucination."
    if " no clear speech " in f" {norm} ":
        return "Ignored non-speech audio description."
    words = norm.split()
    if words and all(word in _FILLER_ONLY_WORDS for word in words):
        return "Ignored filler-only transcript."
    return ""


def clean_transcript(text: str) -> str:
    return _clean_transcript(text)


def transcript_rejection_reason(text: str) -> str:
    return _reject_transcript_reason(text)


def _float_attr(item: Any, name: str) -> float | None:
    try:
        value = getattr(item, name)
    except Exception:
        return None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _segment_rejection_reason(segment: Any, *, relaxed: bool = False) -> str:
    text = str(getattr(segment, "text", "") or "").strip()
    if not text:
        return "empty segment"

    avg_logprob = _float_attr(segment, "avg_logprob")
    no_speech_prob = _float_attr(segment, "no_speech_prob")
    compression_ratio = _float_attr(segment, "compression_ratio")
    start = _float_attr(segment, "start")
    end = _float_attr(segment, "end")
    duration = max(0.0, (end or 0.0) - (start or 0.0)) if start is not None and end is not None else 0.0
    word_count = len(text.split())

    compression_limit = 2.60 if relaxed else 2.35
    logprob_limit = -1.35 if relaxed else -1.10
    if compression_ratio is not None and compression_ratio >= compression_limit:
        return "high compression ratio"
    if avg_logprob is not None and avg_logprob <= logprob_limit:
        return "low log probability"
    if no_speech_prob is not None and avg_logprob is not None:
        hard_no_speech = 0.97 if relaxed else 0.92
        medium_no_speech = 0.88 if relaxed else 0.75
        medium_logprob = -0.80 if relaxed else -0.55
        if no_speech_prob >= hard_no_speech and avg_logprob <= -0.15:
            return "high no-speech probability"
        if no_speech_prob >= medium_no_speech and avg_logprob <= medium_logprob:
            return "high no-speech probability"
        if not relaxed and word_count <= 2 and no_speech_prob >= 0.72 and avg_logprob <= -0.70:
            return "weak short command confidence"
    short_limit = 0.12 if relaxed else 0.20
    short_no_speech = 0.90 if relaxed else 0.75
    if duration and duration < short_limit and word_count <= 2 and no_speech_prob is not None and no_speech_prob >= short_no_speech:
        return "too short and speech probability is weak"
    return ""


def _usable_segment_texts(segments: Any, *, relaxed: bool = False) -> tuple[list[str], int]:
    texts: list[str] = []
    rejected_count = 0
    for segment in segments:
        reason = _segment_rejection_reason(segment, relaxed=relaxed)
        if reason:
            if reason != "empty segment":
                rejected_count += 1
            continue
        text = str(getattr(segment, "text", "") or "").strip()
        if text:
            texts.append(text)
    return texts, rejected_count


def _get_model(model_name: str, compute_type: str) -> object:
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    warnings.filterwarnings("ignore", message="pkg_resources is deprecated as an API.*")
    from faster_whisper import WhisperModel

    key = (model_name, compute_type)
    model = _MODELS.get(key)
    if model is None:
        model = WhisperModel(model_name, device="auto", compute_type=compute_type)
        _MODELS[key] = model
    return model


def _transcribe_pass(model: object, audio: object, *, beam_size: int, relaxed: bool) -> tuple[str, int]:
    segments, _info = model.transcribe(
        audio,
        language="en",
        beam_size=beam_size,
        best_of=1,
        temperature=0.0,
        condition_on_previous_text=False,
        hotwords=_default_hotwords(),
        repetition_penalty=1.1,
        no_repeat_ngram_size=3,
        compression_ratio_threshold=2.7 if relaxed else 2.5,
        log_prob_threshold=-1.5 if relaxed else -1.2,
        no_speech_threshold=0.86 if relaxed else 0.72,
        vad_filter=False,
        hallucination_silence_threshold=1.0,
    )
    segment_texts, rejected_segments = _usable_segment_texts(segments, relaxed=relaxed)
    return _clean_transcript(" ".join(segment_texts)), rejected_segments


def _transcribe_sync(audio_path: Path, model_name: str, compute_type: str) -> str:
    model = _get_model(model_name, compute_type)
    audio = _prepare_audio_input(audio_path)
    text, rejected_segments = _transcribe_pass(
        model,
        audio,
        beam_size=_default_beam_size(),
        relaxed=False,
    )
    rejected = _reject_transcript_reason(text)
    if text and not rejected:
        return text

    retry_text, retry_rejected_segments = _transcribe_pass(
        model,
        audio,
        beam_size=_retry_beam_size(),
        relaxed=True,
    )
    retry_rejected = _reject_transcript_reason(retry_text)
    if retry_text and not retry_rejected:
        return retry_text
    if retry_rejected:
        raise RuntimeError(retry_rejected)
    if rejected:
        raise RuntimeError(rejected)
    if rejected_segments or retry_rejected_segments:
        raise RuntimeError("Ignored low-confidence transcription.")
    raise RuntimeError("No clear speech detected.")


def _extract_worker_payload(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


async def _transcribe_in_subprocess(audio_path: Path, model_name: str, compute_type: str) -> tuple[str, str]:
    env = os.environ.copy()
    env.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    cmd = _transcription_worker_command(
        str(audio_path),
        "--model",
        model_name,
        "--compute-type",
        compute_type,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except Exception as exc:
        return "", str(exc)

    try:
        timeout = _default_request_timeout_seconds()
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return "", f"Local transcription timed out after {timeout:.0f}s."

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace").strip()
    payload = _extract_worker_payload(stdout)
    if payload is not None:
        text = str(payload.get("text", "")).strip()
        err = str(payload.get("error", "")).strip()
        if err:
            return "", err
        if text:
            return text, ""
        return "", "Local transcription returned no text."

    details = stderr or stdout.strip()
    if proc.returncode != 0:
        return "", details or f"Local transcription worker exited with {proc.returncode}."
    return "", details or "Local transcription returned no text."


class _PersistentTranscriptionWorker:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.model_name = ""
        self.compute_type = ""
        self.start_lock = asyncio.Lock()
        self.write_lock = asyncio.Lock()
        self.reader_task: asyncio.Task[None] | None = None
        self.stderr_task: asyncio.Task[None] | None = None
        self.stderr_lines: list[str] = []
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.request_serial = 0

    async def warm(self, model_name: str, compute_type: str) -> str:
        return await self._ensure_started(model_name, compute_type)

    async def transcribe(self, audio_path: Path, model_name: str, compute_type: str) -> tuple[str, str]:
        for attempt in range(2):
            err = await self._ensure_started(model_name, compute_type)
            if err:
                return "", err

            proc = self.process
            if proc is None or proc.stdin is None:
                await self.stop()
                continue

            self.request_serial += 1
            request_id = str(self.request_serial)
            future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self.pending[request_id] = future
            request = json.dumps({"id": request_id, "audio_path": str(audio_path)}, ensure_ascii=False) + "\n"
            try:
                async with self.write_lock:
                    proc.stdin.write(request.encode("utf-8"))
                    await proc.stdin.drain()
                payload = await asyncio.wait_for(asyncio.shield(future), timeout=_default_request_timeout_seconds())
            except asyncio.CancelledError:
                self.pending.pop(request_id, None)
                if not future.done():
                    future.cancel()
                raise
            except asyncio.TimeoutError:
                self.pending.pop(request_id, None)
                if not future.done():
                    future.cancel()
                await self.stop()
                return "", f"Local transcription timed out after {_default_request_timeout_seconds():.0f}s."
            except Exception as exc:
                self.pending.pop(request_id, None)
                if not future.done():
                    future.cancel()
                await self.stop()
                if attempt == 0:
                    continue
                return "", str(exc)

            text = str(payload.get("text", "")).strip()
            error = str(payload.get("error", "")).strip()
            if error:
                return "", error
            if text:
                return text, ""
            return "", "Local transcription returned no text."

        return "", "Local transcription worker is unavailable."

    async def stop(self) -> None:
        async with self.start_lock:
            await self._stop_locked()

    async def _ensure_started(self, model_name: str, compute_type: str) -> str:
        async with self.start_lock:
            if (
                self.process is not None
                and self.process.returncode is None
                and self.model_name == model_name
                and self.compute_type == compute_type
            ):
                return ""

            await self._stop_locked()
            self.stderr_lines.clear()
            env = os.environ.copy()
            env.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
            env.setdefault("PYTHONIOENCODING", "utf-8")
            cmd = _transcription_worker_command(
                "--server",
                "--model",
                model_name,
                "--compute-type",
                compute_type,
            )

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
            except Exception as exc:
                return str(exc)

            self.process = proc
            self.model_name = model_name
            self.compute_type = compute_type
            self.stderr_task = asyncio.create_task(self._drain_stderr(proc))

            try:
                if proc.stdout is None:
                    return "Local transcription worker did not open stdout."
                ready_b = await asyncio.wait_for(proc.stdout.readline(), timeout=_default_timeout_seconds())
            except Exception as exc:
                await self._stop_locked()
                return str(exc)

            payload = _extract_worker_payload(ready_b.decode("utf-8", errors="replace"))
            if payload is None or not payload.get("ready"):
                error = str(payload.get("error", "")).strip() if payload is not None else ""
                await self._stop_locked()
                return error or self._stderr_tail() or "Local transcription worker did not become ready."

            self.reader_task = asyncio.create_task(self._read_responses(proc))
            return ""

    async def _read_responses(self, proc: asyncio.subprocess.Process) -> None:
        if proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                payload = _extract_worker_payload(line.decode("utf-8", errors="replace"))
                if payload is None:
                    continue
                request_id = str(payload.get("id", ""))
                future = self.pending.pop(request_id, None)
                if future is not None and not future.done():
                    future.set_result(payload)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self._fail_pending(str(exc))
            return
        self._fail_pending(self._stderr_tail() or "Local transcription worker stopped unexpectedly.")

    def _fail_pending(self, message: str) -> None:
        pending = list(self.pending.values())
        self.pending.clear()
        for future in pending:
            if not future.done():
                future.set_result({"text": "", "error": message})

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        if proc.stderr is None:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self.stderr_lines.append(text)
                del self.stderr_lines[:-20]

    async def _stop_locked(self) -> None:
        proc = self.process
        self.process = None
        self.model_name = ""
        self.compute_type = ""
        self._fail_pending("Local transcription worker stopped.")

        reader = self.reader_task
        self.reader_task = None
        if reader is not None and reader is not asyncio.current_task() and not reader.done():
            reader.cancel()
            with contextlib.suppress(BaseException):
                await reader

        if proc is not None:
            if proc.returncode is None and proc.stdin is not None:
                with contextlib.suppress(Exception):
                    proc.stdin.close()
                with contextlib.suppress(Exception):
                    await proc.stdin.wait_closed()
            if proc.returncode is None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=0.5)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()

        task = self.stderr_task
        self.stderr_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    def _stderr_tail(self) -> str:
        return "\n".join(self.stderr_lines[-5:]).strip()


def _persistent_worker() -> _PersistentTranscriptionWorker:
    global _PERSISTENT_WORKER
    if _PERSISTENT_WORKER is None:
        _PERSISTENT_WORKER = _PersistentTranscriptionWorker()
    return _PERSISTENT_WORKER


async def warm_local_transcriber(
    model_name: str | None = None,
    compute_type: str | None = None,
) -> str:
    model = model_name or _default_model_name()
    compute = compute_type or _default_compute_type()
    backend = os.environ.get("WIZPR_LOCAL_WHISPER_BACKEND", "server").strip().lower()
    if backend not in {"server", "persistent", "warm"}:
        return ""
    return await _persistent_worker().warm(model, compute)


async def close_local_transcriber() -> None:
    worker = _PERSISTENT_WORKER
    if worker is not None:
        await worker.stop()


async def transcribe_audio_local(
    audio_path: Path,
    model_name: str | None = None,
    compute_type: str | None = None,
) -> tuple[str, str]:
    model = model_name or _default_model_name()
    compute = compute_type or _default_compute_type()
    backend = os.environ.get("WIZPR_LOCAL_WHISPER_BACKEND", "server").strip().lower()
    if backend in {"thread", "inprocess", "direct"}:
        try:
            text = await asyncio.to_thread(_transcribe_sync, audio_path, model, compute)
            if not text:
                return "", "Local transcription returned no text."
            return text, ""
        except Exception as exc:
            return "", str(exc)

    if backend in {"subprocess", "process", "oneshot"}:
        return await _transcribe_in_subprocess(audio_path, model, compute)

    if backend in {"server", "persistent", "warm"}:
        return await _persistent_worker().transcribe(audio_path, model, compute)

    return await _transcribe_in_subprocess(audio_path, model, compute)
