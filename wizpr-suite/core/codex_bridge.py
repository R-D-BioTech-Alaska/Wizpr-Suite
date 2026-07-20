from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import CodexConfig


@dataclass
class CodexRunResult:
    ok: bool
    output: str
    error: str = ""
    executable: str = ""


def detect_codex_executable() -> str:
    env_path = os.environ.get("WIZPR_CODEX_EXE", "").strip()
    if env_path and Path(env_path).exists():
        return env_path

    path_exe = shutil.which("codex")
    if path_exe and "WindowsApps" not in path_exe:
        return path_exe

    candidates: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        root = Path(local_app_data)
        candidates.extend(root.glob("OpenAI/Codex/bin/*/codex.exe"))
        candidates.extend(root.glob("Packages/OpenAI.Codex_*/LocalCache/Local/OpenAI/Codex/bin/codex.exe"))

    candidates = [p for p in candidates if p.exists()]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return str(candidates[0]) if candidates else ""


async def run_codex_prompt(prompt: str, cfg: CodexConfig, default_cwd: Path) -> CodexRunResult:
    executable = cfg.executable.strip() or detect_codex_executable()
    if not executable:
        return CodexRunResult(False, "", "Codex CLI was not found.")

    cwd = Path(cfg.working_dir.strip()) if cfg.working_dir.strip() else default_cwd
    fd, out_name = tempfile.mkstemp(prefix="wizpr_codex_", suffix=".txt")
    os.close(fd)
    out_file = Path(out_name)
    cmd = [
        executable,
        "exec",
        "--color",
        "never",
        "--output-last-message",
        str(out_file),
        "--sandbox",
        cfg.sandbox.strip() or "workspace-write",
        "-C",
        str(cwd),
    ]
    if cfg.model.strip():
        cmd.extend(["--model", cfg.model.strip()])
    cmd.append(prompt)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(),
            timeout=max(1.0, float(cfg.timeout_seconds or 300.0)),
        )
    except asyncio.TimeoutError:
        return CodexRunResult(False, "", "Codex timed out.", executable)
    except Exception as e:
        return CodexRunResult(False, "", str(e), executable)

    stdout = stdout_b.decode("utf-8", errors="replace").strip()
    stderr = stderr_b.decode("utf-8", errors="replace").strip()
    output = ""
    try:
        if out_file.exists():
            output = out_file.read_text(encoding="utf-8").strip()
    finally:
        try:
            out_file.unlink(missing_ok=True)
        except Exception:
            pass

    if not output:
        output = stdout
    if proc.returncode != 0:
        return CodexRunResult(False, output, stderr or f"Codex exited with {proc.returncode}.", executable)
    return CodexRunResult(True, output, stderr, executable)
