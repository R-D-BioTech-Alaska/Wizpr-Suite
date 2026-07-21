from __future__ import annotations

import asyncio
import os
import re
import subprocess
import webbrowser

from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class DesktopToolRequest:
    tool: str
    label: str
    original: str
    target: str = ""

_OPEN_RE = re.compile(r"\b(?:open|launch|start)\b", re.IGNORECASE)

def parse_desktop_tool_request(text: str) -> DesktopToolRequest | None:
    command = " ".join((text or "").strip().split())
    if not command or not _OPEN_RE.search(command):
        return None
    lowered = command.casefold()
    choices = (
        (("notepad", "text editor"), "notepad", "Notepad"),
        (("calculator", "calc"), "calculator", "Calculator"),
        (("file explorer", "windows explorer", "explorer"), "explorer", "File Explorer"),
        (("windows settings", "settings"), "settings", "Windows Settings"),
        (("command prompt", "cmd"), "cmd", "Command Prompt"),
        (("powershell",), "powershell", "PowerShell"),
        (("terminal", "windows terminal"), "terminal", "Windows Terminal"),
        (("microsoft edge", "edge"), "edge", "Microsoft Edge"),
        (("google chrome", "chrome"), "chrome", "Google Chrome"),
        (("browser", "web browser", "internet"), "browser", "Web Browser"),
        (("downloads", "downloads folder"), "folder", "Downloads", "downloads"),
        (("documents", "documents folder"), "folder", "Documents", "documents"),
        (("desktop folder",), "folder", "Desktop", "desktop"),
    )
    for item in choices:
        aliases, tool, label, *target = item
        if any(alias in lowered for alias in aliases):
            return DesktopToolRequest(tool=tool, label=label, original=command, target=target[0] if target else "")
    return None

async def execute_desktop_tool(request: DesktopToolRequest) -> tuple[bool, str]:
    if os.name != "nt":
        return False, "Desktop tools currently require Windows."

    def _launch() -> None:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        if request.tool == "browser":
            webbrowser.open("about:blank", new=1)
            return
        if request.tool == "folder":
            base = Path.home()
            target = {
                "downloads": base / "Downloads",
                "documents": base / "Documents",
                "desktop": base / "Desktop",
            }.get(request.target, base)
            subprocess.Popen(["explorer.exe", str(target)], creationflags=flags, close_fds=True)
            return
        command = {
            "notepad": ["notepad.exe"],
            "calculator": ["calc.exe"],
            "explorer": ["explorer.exe"],
            "settings": ["cmd.exe", "/c", "start", "", "ms-settings:"],
            "cmd": ["cmd.exe"],
            "powershell": ["powershell.exe", "-NoProfile"],
            "terminal": ["wt.exe"],
            "edge": ["msedge.exe"],
            "chrome": ["chrome.exe"],
        }.get(request.tool)
        if not command:
            raise RuntimeError(f"Unsupported desktop tool: {request.tool}")
        subprocess.Popen(command, creationflags=flags, close_fds=True)

    try:
        await asyncio.to_thread(_launch)
        return True, f"Opened {request.label}."
    except FileNotFoundError:
        if request.tool in {"chrome", "edge", "terminal"}:
            return False, f"{request.label} is not installed or could not be found."
        return False, f"Could not find {request.label}."
    except Exception as exc:
        return False, str(exc)
