from __future__ import annotations

import asyncio
import ipaddress
import secrets
import socket
import time
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from .config import DEFAULT_MAPPINGS, MobileBridgeConfig
from .event_bus import EventBus

BridgeCommandHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
BridgeStatusProvider = Callable[[], dict[str, Any]]

BRIDGE_TARGETS = {"assistant", "clipboard", "codex", "opencode", "paste", "transcript"}
BRIDGE_EVENT_TOPICS = [
    "ring_event",
    "ring_command",
    "button_single",
    "button_double",
    "button_triple",
    "button_quad",
    "button_five",
    "button_long",
    "button_multi",
    "sos",
    "battery",
    "version",
    "proxy",
    "mic_pre_on",
    "mic_on",
    "mic_off",
    "audio_capture",
    "sleep",
    "lock",
    "power_off",
    "bridge_command_queued",
    "bridge_command_result",
]
BRIDGE_ENDPOINTS = [
    "GET /",
    "GET /app",
    "GET /health",
    "GET /status",
    "GET /capabilities",
    "GET /events",
    "WS /events",
    "POST /commands",
    "GET /commands/{id}",
]
BRIDGE_COMMAND_EXAMPLES = [
    {"target": "assistant", "text": "Wizpr, summarize this"},
    {"target": "codex", "text": "Codex, inspect the current file"},
    {"target": "opencode", "text": "OpenCode, run the tests"},
    {"target": "transcript", "text": "Save this as the current transcript"},
    {"target": "clipboard", "text": "Copy this text"},
    {"target": "paste", "text": "Voice keyboard text"},
]


def bridge_page_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wizpr Suite Bridge</title>
<style>
body { margin: 0; font: 16px system-ui, -apple-system, Segoe UI, sans-serif; background: #111318; color: #eef2f7; }
main { max-width: 760px; margin: 0 auto; padding: 18px; }
h1 { font-size: 24px; margin: 0 0 14px; }
label { display: block; margin: 12px 0 6px; color: #cbd5e1; }
select, input, textarea, button { box-sizing: border-box; width: 100%; border: 1px solid #334155; border-radius: 8px; background: #171a21; color: #eef2f7; padding: 10px; font: inherit; }
textarea { min-height: 120px; resize: vertical; }
button { margin-top: 12px; background: #2563eb; border-color: #3b82f6; font-weight: 650; }
button:disabled { opacity: .6; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.status { margin: 14px 0; color: #a7f3d0; min-height: 22px; }
pre { white-space: pre-wrap; word-break: break-word; border: 1px solid #334155; border-radius: 8px; background: #0f172a; padding: 10px; min-height: 180px; max-height: 340px; overflow: auto; }
@media (max-width: 640px) { .row { grid-template-columns: 1fr; } main { padding: 14px; } }
</style>
</head>
<body>
<main>
<h1>Wizpr Suite Bridge</h1>
<div class="row">
<div>
<label for="target">Target</label>
<select id="target">
<option value="assistant">Assistant</option>
<option value="clipboard">Copy Text</option>
<option value="codex">Codex</option>
<option value="opencode">OpenCode</option>
<option value="paste">Voice Keyboard</option>
<option value="transcript">Transcript Only</option>
</select>
</div>
<div>
<label for="token">Token</label>
<input id="token" autocomplete="off" placeholder="Only needed for non-local bridge">
</div>
</div>
<label for="text">Command</label>
<textarea id="text" placeholder="Type a command for Wizpr Suite"></textarea>
<button id="send">Send</button>
<div id="status" class="status"></div>
<label for="desktop-status">Desktop Status</label>
<pre id="desktop-status"></pre>
<label for="events">Events</label>
<pre id="events"></pre>
</main>
<script>
const target = document.getElementById("target");
const token = document.getElementById("token");
const text = document.getElementById("text");
const send = document.getElementById("send");
const statusBox = document.getElementById("status");
const desktopStatusBox = document.getElementById("desktop-status");
const eventsBox = document.getElementById("events");
const query = new URL(location.href).searchParams;
token.value = query.get("token") || localStorage.getItem("wizprBridgeToken") || "";
const pendingCommands = new Set();
function authHeaders(json) {
  const headers = json ? {"Content-Type": "application/json"} : {};
  if (token.value.trim()) headers["X-Wizpr-Token"] = token.value.trim();
  return headers;
}
function show(value) {
  const line = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  eventsBox.textContent = `${line}\n${eventsBox.textContent}`.slice(0, 12000);
}
function targetLabel(value) {
  return {
    assistant: "Assistant",
    clipboard: "Copy Text",
    codex: "Codex",
    opencode: "OpenCode",
    paste: "Voice Keyboard",
    transcript: "Transcript Only",
  }[value] || value;
}
async function refreshCapabilities() {
  try {
    const res = await fetch("/capabilities", {headers: authHeaders(false)});
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const body = await res.json();
    if (Array.isArray(body.targets)) {
      const current = target.value;
      target.innerHTML = "";
      for (const value of body.targets) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = targetLabel(value);
        target.appendChild(option);
      }
      if (body.targets.includes(current)) target.value = current;
    }
  } catch (err) {
    show(`Capabilities unavailable: ${err}`);
  }
}
async function refreshDesktopStatus() {
  try {
    const res = await fetch("/status", {headers: authHeaders(false)});
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const body = await res.json();
    desktopStatusBox.textContent = JSON.stringify(body, null, 2);
  } catch (err) {
    desktopStatusBox.textContent = `Status unavailable: ${err}`;
  }
}
async function checkCommand(id) {
  if (!id) return;
  try {
    const res = await fetch(`/commands/${encodeURIComponent(id)}`, {headers: authHeaders(false)});
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `${res.status} ${res.statusText}`);
    if (data.state === "done") {
      pendingCommands.delete(id);
      statusBox.textContent = data.result && data.result.ok === false ? "Rejected or failed." : "Completed.";
      show(data);
    }
  } catch (err) {
    statusBox.textContent = `Status check failed: ${err}`;
  }
}
setInterval(() => {
  for (const id of pendingCommands) checkCommand(id);
}, 2500);
setInterval(refreshDesktopStatus, 4000);
async function refreshEvents() {
  try {
    const res = await fetch("/events", {headers: authHeaders(false)});
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const body = await res.json();
    eventsBox.textContent = (body.events || []).reverse().map(e => JSON.stringify(e)).join("\n");
  } catch (err) {
    statusBox.textContent = `Events unavailable: ${err}`;
  }
}
function connectEvents() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const qs = token.value.trim() ? `?token=${encodeURIComponent(token.value.trim())}` : "";
  const ws = new WebSocket(`${scheme}://${location.host}/events${qs}`);
  ws.onmessage = event => {
    const data = JSON.parse(event.data);
    show(data);
    if (data.topic === "bridge_command_result" && data.payload && data.payload.id) {
      pendingCommands.delete(data.payload.id);
      statusBox.textContent = data.payload.ok === false ? "Rejected or failed." : "Completed.";
    }
  };
  ws.onclose = () => setTimeout(connectEvents, 2500);
  ws.onerror = () => ws.close();
}
send.onclick = async () => {
  const body = {target: target.value, source: "bridge-page", text: text.value.trim()};
  if (!body.text) {
    statusBox.textContent = "Command text is required.";
    return;
  }
  localStorage.setItem("wizprBridgeToken", token.value.trim());
  send.disabled = true;
  statusBox.textContent = "Sending...";
  try {
    const res = await fetch("/commands", {method: "POST", headers: authHeaders(true), body: JSON.stringify(body)});
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `${res.status} ${res.statusText}`);
    if (data.queued) {
      pendingCommands.add(data.id);
      statusBox.textContent = "Queued for approval in Wizpr Suite.";
    } else {
      statusBox.textContent = data.result && data.result.ok === false ? "Failed." : "Completed.";
    }
    show(data);
  } catch (err) {
    statusBox.textContent = `Send failed: ${err}`;
  } finally {
    send.disabled = false;
  }
};
refreshCapabilities();
refreshDesktopStatus();
refreshEvents();
connectEvents();
</script>
</body>
</html>"""


def make_bridge_token() -> str:
    return secrets.token_urlsafe(24)


def bridge_url(cfg: MobileBridgeConfig, local_hosts: list[str] | None = None) -> str:
    host = (cfg.host or "127.0.0.1").strip()
    port = int(cfg.port or 8844)
    if _is_bind_all_host(host):
        hosts = _bridge_lan_hosts(local_hosts)
        shown_host = hosts[0] if hosts else "127.0.0.1"
    else:
        shown_host = host
    shown_host = shown_host.strip("[]")
    if ":" in shown_host and not shown_host.startswith("["):
        shown_host = f"[{shown_host}]"
    return f"http://{shown_host}:{port}"


def bridge_app_url(
    cfg: MobileBridgeConfig,
    *,
    include_token: bool = False,
    local_hosts: list[str] | None = None,
) -> str:
    url = bridge_url(cfg, local_hosts=local_hosts).rstrip("/") + "/app"
    token = (cfg.token or "").strip()
    if include_token and token and bridge_needs_token(cfg):
        url += f"?token={quote(token, safe='')}"
    return url


def bridge_needs_token(cfg: MobileBridgeConfig) -> bool:
    host = (cfg.host or "").strip().lower()
    return host not in {"", "127.0.0.1", "localhost", "::1"}


def _is_bind_all_host(host: str) -> bool:
    return (host or "").strip().strip("[]").lower() in {"0.0.0.0", "::"}


def _bridge_lan_hosts(local_hosts: list[str] | None = None) -> list[str]:
    candidates = local_hosts if local_hosts is not None else _detect_bridge_lan_hosts()
    out: list[str] = []
    for host in candidates:
        host = (host or "").strip().strip("[]")
        if host and host not in out and _is_reachable_client_host(host):
            out.append(host)
    return out


def _detect_bridge_lan_hosts() -> list[str]:
    out: list[str] = []

    def add(value: str) -> None:
        value = (value or "").strip()
        if value and value not in out:
            out.append(value)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            add(sock.getsockname()[0])
    except Exception:
        pass

    try:
        for addr in socket.gethostbyname_ex(socket.gethostname())[2]:
            add(addr)
    except Exception:
        pass

    return out


def _is_reachable_client_host(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast)


class MobileBridge:
    def __init__(
        self,
        cfg: MobileBridgeConfig,
        bus: EventBus,
        command_handler: BridgeCommandHandler | None = None,
        status_provider: BridgeStatusProvider | None = None,
    ) -> None:
        self.cfg = cfg
        self.bus = bus
        self.command_handler = command_handler
        self.status_provider = status_provider
        self.events: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []
        self.command_results: dict[str, dict[str, Any]] = {}
        self._server: Any = None
        self._task: asyncio.Task[Any] | None = None
        self._websockets: set[Any] = set()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> str:
        if self.running:
            return bridge_url(self.cfg)
        if bridge_needs_token(self.cfg) and not self.cfg.token:
            raise RuntimeError("A bridge token is required when binding outside localhost.")

        app = self._build_app()
        try:
            import uvicorn
        except Exception as exc:
            raise RuntimeError(f"uvicorn is not installed: {exc}") from exc

        config = uvicorn.Config(
            app,
            host=(self.cfg.host or "127.0.0.1").strip(),
            port=int(self.cfg.port or 8844),
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        await asyncio.sleep(0.15)
        if self._task.done():
            err = self._task.exception()
            raise RuntimeError(str(err or "Bridge server stopped during startup."))
        return bridge_url(self.cfg)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        task = self._task
        self._task = None
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=3.0)
            except asyncio.TimeoutError:
                task.cancel()
        self._server = None
        for ws in list(self._websockets):
            try:
                await ws.close()
            except Exception:
                pass
        self._websockets.clear()

    async def publish_event(self, topic: str, payload: Any) -> None:
        event = {
            "ts": time.time(),
            "topic": topic,
            "payload": payload,
        }
        if topic == "bridge_command_result" and isinstance(payload, dict):
            request_id = str(payload.get("id") or "").strip()
            if request_id:
                self.command_results[request_id] = dict(payload)
                if len(self.command_results) > 200:
                    for key in list(self.command_results)[:-200]:
                        self.command_results.pop(key, None)
        self.events.append(event)
        del self.events[:-200]

        for ws in list(self._websockets):
            try:
                await ws.send_json(event)
            except Exception:
                self._websockets.discard(ws)

    def take_pending(self, request_id: str) -> dict[str, Any] | None:
        for idx, item in enumerate(self.pending):
            if item.get("id") == request_id:
                return self.pending.pop(idx)
        return None

    def _build_app(self) -> Any:
        try:
            from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
            from fastapi.middleware.cors import CORSMiddleware
            from fastapi.responses import HTMLResponse
        except Exception as exc:
            raise RuntimeError(f"fastapi is not installed: {exc}") from exc

        app = FastAPI(title="Wizpr Suite Bridge", version="2.0.2")
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Wizpr-Token"],
        )

        @app.get("/", response_class=HTMLResponse)
        async def bridge_home() -> str:
            return bridge_page_html()

        @app.get("/app", response_class=HTMLResponse)
        async def bridge_app() -> str:
            return bridge_page_html()

        def check_token(
            authorization: str | None = None,
            x_wizpr_token: str | None = None,
            query_token: str | None = None,
        ) -> None:
            token = (self.cfg.token or "").strip()
            if not token and not bridge_needs_token(self.cfg):
                return
            supplied = (x_wizpr_token or query_token or "").strip()
            if authorization and authorization.lower().startswith("bearer "):
                supplied = authorization[7:].strip()
            if not token or not secrets.compare_digest(token, supplied):
                raise HTTPException(status_code=401, detail="Bridge token required.")

        @app.get("/health")
        async def health() -> dict[str, Any]:
            return {
                "ok": True,
                "name": "Wizpr Suite",
                "approval_required": bool(self.cfg.require_approval),
                "pending_commands": len(self.pending),
                "targets": sorted(BRIDGE_TARGETS),
            }

        @app.get("/status")
        async def status(
            authorization: str | None = Header(default=None),
            x_wizpr_token: str | None = Header(default=None),
        ) -> dict[str, Any]:
            check_token(authorization=authorization, x_wizpr_token=x_wizpr_token)
            desktop: dict[str, Any] = {}
            if self.status_provider is not None:
                try:
                    desktop = dict(self.status_provider())
                except Exception as exc:
                    desktop = {"error": str(exc)}
            return {
                "ok": True,
                "name": "Wizpr Suite",
                "bridge": {
                    "running": self.running,
                    "approval_required": bool(self.cfg.require_approval),
                    "pending_commands": len(self.pending),
                    "events": len(self.events),
                },
                "desktop": desktop,
            }

        @app.get("/capabilities")
        async def capabilities(
            authorization: str | None = Header(default=None),
            x_wizpr_token: str | None = Header(default=None),
        ) -> dict[str, Any]:
            check_token(authorization=authorization, x_wizpr_token=x_wizpr_token)
            return {
                "ok": True,
                "name": "Wizpr Suite",
                "targets": sorted(BRIDGE_TARGETS),
                "events": list(BRIDGE_EVENT_TOPICS),
                "actions": sorted(DEFAULT_MAPPINGS),
                "endpoints": list(BRIDGE_ENDPOINTS),
                "command_examples": list(BRIDGE_COMMAND_EXAMPLES),
                "approval_required": bool(self.cfg.require_approval),
                "pending_commands": len(self.pending),
            }

        @app.get("/events")
        async def recent_events(
            authorization: str | None = Header(default=None),
            x_wizpr_token: str | None = Header(default=None),
        ) -> dict[str, Any]:
            check_token(authorization=authorization, x_wizpr_token=x_wizpr_token)
            return {"events": self.events[-50:]}

        @app.post("/commands")
        async def command(
            body: dict[str, Any],
            authorization: str | None = Header(default=None),
            x_wizpr_token: str | None = Header(default=None),
        ) -> dict[str, Any]:
            check_token(authorization=authorization, x_wizpr_token=x_wizpr_token)
            target = str(body.get("target") or "assistant").strip().lower()
            text = str(body.get("text") or "").strip()
            if target not in BRIDGE_TARGETS:
                raise HTTPException(status_code=400, detail="Unknown command target.")
            if not text:
                raise HTTPException(status_code=400, detail="Command text is required.")

            request = {
                "id": secrets.token_hex(8),
                "ts": time.time(),
                "source": str(body.get("source") or "mobile").strip() or "mobile",
                "target": target,
                "text": text,
            }
            if self.cfg.require_approval or self.command_handler is None:
                self.pending.append(request)
                del self.pending[:-100]
                await self.publish_event("bridge_command_queued", request)
                await self.bus.publish("bridge_request", request)
                return {"queued": True, "id": request["id"], "approval_required": True}

            result = await self.command_handler(request)
            await self.publish_event("bridge_command_result", {"id": request["id"], "target": target, **result})
            return {"queued": False, "id": request["id"], "result": result}

        @app.get("/commands/{request_id}")
        async def command_status(
            request_id: str,
            authorization: str | None = Header(default=None),
            x_wizpr_token: str | None = Header(default=None),
        ) -> dict[str, Any]:
            check_token(authorization=authorization, x_wizpr_token=x_wizpr_token)
            request_id = str(request_id or "").strip()
            if not request_id:
                raise HTTPException(status_code=400, detail="Command id is required.")
            for item in self.pending:
                if item.get("id") == request_id:
                    return {"id": request_id, "state": "pending", "request": item}
            result = self.command_results.get(request_id)
            if result is not None:
                return {"id": request_id, "state": "done", "result": result}
            raise HTTPException(status_code=404, detail="Command id was not found.")

        @app.websocket("/events")
        async def events_ws(websocket: WebSocket) -> None:
            query_token = websocket.query_params.get("token")
            try:
                check_token(query_token=query_token)
            except HTTPException:
                await websocket.close(code=1008)
                return
            await websocket.accept()
            self._websockets.add(websocket)
            try:
                for event in self.events[-20:]:
                    await websocket.send_json(event)
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                pass
            finally:
                self._websockets.discard(websocket)

        return app
