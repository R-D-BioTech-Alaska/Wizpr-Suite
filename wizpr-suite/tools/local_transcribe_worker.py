from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ..core.local_transcription import _default_compute_type, _default_model_name, _get_model, _transcribe_sync

def _error_message(exc: BaseException) -> str:
    if isinstance(exc, RuntimeError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"

def _write(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()

def _finish(payload: dict[str, object], code: int) -> None:
    _write(payload)
    sys.stderr.flush()
    os._exit(code)

def _serve(model_name: str, compute_type: str) -> None:
    try:
        _get_model(model_name, compute_type)
    except BaseException as exc:
        _finish({"ready": False, "error": _error_message(exc)}, 1)

    _write({"ready": True, "error": ""})
    for line in sys.stdin:
        try:
            request = json.loads(line)
            request_id = str(request.get("id", ""))
            audio_path = Path(str(request.get("audio_path", "")))
            if not audio_path.exists():
                _write({"id": request_id, "text": "", "error": f"Audio file does not exist: {audio_path}"})
                continue
            text = _transcribe_sync(audio_path, model_name, compute_type)
        except BaseException as exc:
            request_id = str(locals().get("request_id", ""))
            _write({"id": request_id, "text": "", "error": _error_message(exc)})
            continue

        if not text:
            _write({"id": request_id, "text": "", "error": "Local transcription returned no text."})
        else:
            _write({"id": request_id, "text": text, "error": ""})

    os._exit(0)

def main() -> None:
    parser = argparse.ArgumentParser(description="Transcribe an audio file with local faster-whisper.")
    parser.add_argument("audio_path", nargs="?")
    parser.add_argument("--server", action="store_true")
    parser.add_argument("--model", default="")
    parser.add_argument("--compute-type", default="")
    args = parser.parse_args()

    model_name = args.model.strip() or _default_model_name()
    compute_type = args.compute_type.strip() or _default_compute_type()
    if args.server:
        _serve(model_name, compute_type)
        return

    if not args.audio_path:
        _finish({"text": "", "error": "Audio path is required."}, 1)

    audio_path = Path(args.audio_path)
    if not audio_path.exists():
        _finish({"text": "", "error": f"Audio file does not exist: {audio_path}"}, 1)

    try:
        text = _transcribe_sync(audio_path, model_name, compute_type)
    except BaseException as exc:
        _finish({"text": "", "error": _error_message(exc)}, 1)

    if not text:
        _finish({"text": "", "error": "Local transcription returned no text."}, 1)
    _finish({"text": text, "error": ""}, 0)

if __name__ == "__main__":
    main()
