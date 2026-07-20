from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import OpenCodeConfig


@dataclass
class OpenCodeRunResult:
    ok: bool
    output: str
    error: str = ""
    executable: str = ""


def detect_opencode_executable() -> str:
    env_path = os.environ.get("WIZPR_OPENCODE_EXE", "").strip()
    if env_path and Path(env_path).exists():
        return env_path

    for name in ("opencode.cmd", "opencode.exe", "opencode"):
        found = shutil.which(name)
        if found:
            return found

    appdata = os.environ.get("APPDATA", "")
    if appdata:
        npm_dir = Path(appdata) / "npm"
        for name in ("opencode.cmd", "opencode.exe", "opencode"):
            candidate = npm_dir / name
            if candidate.exists():
                return str(candidate)

    return ""


async def list_opencode_models(executable: str = "") -> tuple[list[str], str]:
    exe = executable.strip() or detect_opencode_executable()
    if not exe:
        return [], "OpenCode CLI was not found."

    try:
        proc = await asyncio.create_subprocess_exec(
            exe,
            "models",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except Exception as exc:
        return [], str(exc)

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        return [], stderr or f"OpenCode exited with {proc.returncode}."

    models = sort_opencode_models(_parse_model_lines(stdout))
    return models, ""


def _parse_model_lines(stdout: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in stdout.splitlines():
        for token in re.split(r"\s+", line.strip()):
            model = token.strip("`'\"|,")
            if "/" not in model or model.startswith(("http://", "https://")):
                continue
            if model.casefold() in {"provider/model", "provider/modelid"}:
                continue
            if model not in seen:
                seen.add(model)
                out.append(model)
    return out


def sort_opencode_models(models: list[str]) -> list[str]:
    return sorted(set(models), key=_opencode_model_sort_key)


def _opencode_model_sort_key(model: str) -> tuple[int, str]:
    text = model.casefold()
    score = 50
    if text.startswith("ollama/"):
        score -= 10
    if "qwen" in text:
        score -= 20
    if "coder" in text or "code" in text:
        score -= 8
    if "qwen36-27b" in text:
        score -= 10
    return score, text


async def run_opencode_prompt(prompt: str, cfg: OpenCodeConfig, default_cwd: Path) -> OpenCodeRunResult:
    executable = cfg.executable.strip() or detect_opencode_executable()
    if not executable:
        return OpenCodeRunResult(False, "", "OpenCode CLI was not found.")

    cwd = Path(cfg.working_dir.strip()) if cfg.working_dir.strip() else default_cwd
    message = (
        f"{prompt.strip()}\n\n"
        "Context: this request came from Wizpr Suite ring voice input. "
        "Treat the transcript above as the user's command for the configured workspace. "
        "Make the requested change or run the requested task, then reply with the concise result."
    )

    cmd = [executable, "run", message, "--dir", str(cwd)]
    if cfg.model.strip():
        cmd.extend(["--model", cfg.model.strip()])
    if cfg.continue_session:
        cmd.append("--continue")
    if cfg.auto_approve:
        cmd.append("--auto")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(),
            timeout=max(1.0, float(cfg.timeout_seconds or 600.0)),
        )
    except asyncio.TimeoutError:
        return OpenCodeRunResult(False, "", "OpenCode timed out.", executable)
    except Exception as exc:
        return OpenCodeRunResult(False, "", str(exc), executable)

    stdout = stdout_b.decode("utf-8", errors="replace").strip()
    stderr = stderr_b.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        return OpenCodeRunResult(False, stdout, stderr or f"OpenCode exited with {proc.returncode}.", executable)
    return OpenCodeRunResult(True, stdout, stderr, executable)
