from __future__ import annotations

import asyncio
import contextlib
import os
import re

_VOICE_PREFERENCE = (
    "Microsoft Aria",
    "Microsoft Jenny",
    "Microsoft Zira Desktop",
    "Microsoft Zira",
    "Microsoft Guy",
    "Microsoft David Desktop",
    "Microsoft David",
)


def text_for_speech(text: str, max_chars: int = 1400) -> str:
    cleaned = text.strip()
    if not cleaned:
        return ""

    cleaned = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", cleaned)
    cleaned = re.sub(r"```.*?```", " code omitted. ", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = re.sub(r"https?://\S+", "link", cleaned)
    cleaned = re.sub(r"(?is)\bTraceback \(most recent call last\):.*", " error details omitted. ", cleaned)

    lines: list[str] = []
    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.casefold()
        if line.startswith(("::", "```")):
            continue
        if re.match(r"^>\s*\[[^\]]+\]", line):
            continue
        if re.match(r"^\[[^\]]*(?:error|warning|voice|transcript|debug|info)[^\]]*\]", line, flags=re.IGNORECASE):
            continue
        if lowered.startswith(("usage:", "ps ", "c:\\", "at line:", "file \"")):
            continue
        if "|" in line and re.match(r"^\|?[\s:|_-]+\|?$", line):
            continue
        if line.count("|") >= 2:
            continue
        line = re.sub(r"^#{1,6}\s+", "", line)
        line = re.sub(r"^\s*[-*+]\s+", "", line)
        line = re.sub(r"^\s*\d+\.\s+", "", line)
        line = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)
        line = re.sub(r"\*([^*]+)\*", r"\1", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
        if line:
            lines.append(line)

    cleaned = " ".join(lines)
    cleaned = " ".join(cleaned.split())
    cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)
    cleaned = re.sub(r"([,.!?;:]){2,}", r"\1", cleaned)
    if not cleaned:
        return ""

    if len(cleaned) <= max_chars:
        return cleaned

    clipped = cleaned[:max_chars].rsplit(" ", 1)[0].strip()
    return clipped + "."


async def speak_text(text: str, voice: str = "", rate: int = 0) -> tuple[bool, str]:
    if os.name != "nt":
        return False, "Spoken responses currently use Windows speech."

    spoken = text_for_speech(text)
    if not spoken:
        return True, ""

    proc: asyncio.subprocess.Process | None = None
    try:
        ps = (
            "$utf8 = New-Object System.Text.UTF8Encoding($false); "
            "[Console]::InputEncoding = $utf8; "
            "Add-Type -AssemblyName System.Speech; "
            "$text = [Console]::In.ReadToEnd(); "
            "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$synth.SetOutputToDefaultAudioDevice(); "
            "$voice = if ($args.Count -ge 1) { [string]$args[0] } else { '' }; "
            "if ($voice) { try { $synth.SelectVoice($voice) } catch {} } "
            "else { "
            "  $prefs = @("
            + ",".join(f"'{name}'" for name in _VOICE_PREFERENCE)
            + "); "
            "  $installed = $synth.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }; "
            "  foreach ($pref in $prefs) { "
            "    $match = $installed | Where-Object { $_ -like \"$pref*\" } | Select-Object -First 1; "
            "    if ($match) { try { $synth.SelectVoice($match) } catch {}; break } "
            "  } "
            "}; "
            "$rate = 0; "
            "if ($args.Count -ge 2) { try { $rate = [int]$args[1] } catch { $rate = 0 } }; "
            "$synth.Rate = [Math]::Max(-10, [Math]::Min(10, $rate)); "
            "$synth.Volume = 100; "
            "$synth.Speak($text); "
            "$synth.Dispose();"
        )
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            ps,
            voice.strip(),
            str(int(rate)),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=0x08000000,
        )
        stdout_b, stderr_b = await proc.communicate(spoken.encode("utf-8"))
        if proc.returncode != 0:
            msg = stderr_b.decode("utf-8", errors="replace").strip()
            if not msg:
                msg = stdout_b.decode("utf-8", errors="replace").strip()
            return False, msg or f"Windows speech exited with {proc.returncode}."
        return True, ""
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(Exception):
                await proc.wait()
        raise
    except Exception as exc:
        return False, str(exc)


async def play_feedback_sound(kind: str) -> tuple[bool, str]:
    if os.name != "nt":
        return False, "Feedback sounds currently use Windows system sounds."

    def _play() -> None:
        import winsound

        sound = {
            "connect": winsound.MB_ICONASTERISK,
            "mic_on": winsound.MB_OK,
            "mic_off": winsound.MB_ICONQUESTION,
        }.get(kind, winsound.MB_OK)
        winsound.MessageBeep(sound)

    try:
        await asyncio.to_thread(_play)
        return True, ""
    except Exception as exc:
        return False, str(exc)
