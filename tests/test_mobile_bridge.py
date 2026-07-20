from __future__ import annotations

import asyncio
import unittest

from fastapi.testclient import TestClient

from wizpr_suite.core.config import MobileBridgeConfig
from wizpr_suite.core.event_bus import EventBus
from wizpr_suite.core.mobile_bridge import MobileBridge, bridge_app_url, bridge_needs_token, bridge_page_html, bridge_url


class MobileBridgeTests(unittest.TestCase):
    def test_localhost_bridge_does_not_need_token(self) -> None:
        cfg = MobileBridgeConfig(host="127.0.0.1", port=8844)

        self.assertFalse(bridge_needs_token(cfg))
        self.assertEqual("http://127.0.0.1:8844", bridge_url(cfg))

    def test_public_bind_needs_token(self) -> None:
        cfg = MobileBridgeConfig(host="0.0.0.0", port=8844)

        self.assertTrue(bridge_needs_token(cfg))
        self.assertEqual("http://192.168.1.50:8844", bridge_url(cfg, local_hosts=["192.168.1.50"]))

    def test_public_bridge_app_url_can_include_token(self) -> None:
        cfg = MobileBridgeConfig(host="0.0.0.0", port=8844, token="secret value")

        self.assertEqual(
            "http://10.0.0.12:8844/app?token=secret%20value",
            bridge_app_url(cfg, include_token=True, local_hosts=["127.0.0.1", "10.0.0.12"]),
        )

    def test_public_bind_falls_back_to_loopback_when_lan_host_is_unknown(self) -> None:
        cfg = MobileBridgeConfig(host="0.0.0.0", port=8844)

        self.assertEqual("http://127.0.0.1:8844", bridge_url(cfg, local_hosts=[]))

    def test_command_queues_for_approval(self) -> None:
        bridge = MobileBridge(MobileBridgeConfig(require_approval=True), EventBus())
        client = TestClient(bridge._build_app())

        result = client.post(
            "/commands",
            json={"target": "codex", "source": "phone", "text": "Codex, open the Wizpr Suite files."},
        )

        self.assertEqual(200, result.status_code)
        self.assertTrue(result.json()["queued"])
        self.assertEqual(1, len(bridge.pending))
        self.assertEqual("codex", bridge.pending[0]["target"])

    def test_command_status_tracks_pending_and_done_results(self) -> None:
        bridge = MobileBridge(MobileBridgeConfig(require_approval=True), EventBus())
        client = TestClient(bridge._build_app())

        queued = client.post("/commands", json={"target": "assistant", "text": "Hello"}).json()
        request_id = queued["id"]
        pending = client.get(f"/commands/{request_id}")

        self.assertEqual("pending", pending.json()["state"])

        bridge.take_pending(request_id)
        asyncio.run(
            bridge.publish_event(
                "bridge_command_result",
                {"id": request_id, "target": "assistant", "ok": False, "error": "Rejected"},
            )
        )
        done = client.get(f"/commands/{request_id}")

        self.assertEqual("done", done.json()["state"])
        self.assertFalse(done.json()["result"]["ok"])

    def test_no_approval_command_stores_result_status(self) -> None:
        async def handle(request: dict) -> dict:
            return {"ok": True, "text": f"handled {request['target']}"}

        bridge = MobileBridge(MobileBridgeConfig(require_approval=False), EventBus(), handle)
        client = TestClient(bridge._build_app())

        result = client.post("/commands", json={"target": "opencode", "text": "Check status"})

        body = result.json()
        self.assertFalse(body["queued"])
        self.assertEqual("handled opencode", body["result"]["text"])
        self.assertEqual("bridge_command_result", bridge.events[-1]["topic"])
        status = client.get(f"/commands/{body['id']}")
        self.assertEqual("done", status.json()["state"])

    def test_bridge_page_is_available_for_phone_clients(self) -> None:
        bridge = MobileBridge(MobileBridgeConfig(), EventBus())
        client = TestClient(bridge._build_app())

        result = client.get("/")

        self.assertEqual(200, result.status_code)
        self.assertIn("text/html", result.headers["content-type"])
        self.assertIn("Wizpr Suite Bridge", result.text)
        self.assertIn("/commands", result.text)
        self.assertIn("/commands/${encodeURIComponent(id)}", result.text)
        self.assertIn("/capabilities", result.text)
        self.assertIn("/status", result.text)
        self.assertIn("Copy Text", result.text)
        self.assertIn("OpenCode", result.text)
        self.assertIn("Voice Keyboard", result.text)
        self.assertIn("bridge-page", bridge_page_html())

    def test_status_endpoint_includes_bridge_and_desktop_state(self) -> None:
        bridge = MobileBridge(
            MobileBridgeConfig(require_approval=True),
            EventBus(),
            status_provider=lambda: {"next_step": "Press the ring button.", "voice": {"target": "codex"}},
        )
        client = TestClient(bridge._build_app())

        result = client.get("/status")

        self.assertEqual(200, result.status_code)
        body = result.json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["bridge"]["running"])
        self.assertTrue(body["bridge"]["approval_required"])
        self.assertEqual("Press the ring button.", body["desktop"]["next_step"])
        self.assertEqual("codex", body["desktop"]["voice"]["target"])

    def test_capabilities_endpoint_lists_targets_events_and_endpoints(self) -> None:
        bridge = MobileBridge(MobileBridgeConfig(require_approval=True), EventBus())
        client = TestClient(bridge._build_app())

        result = client.get("/capabilities")

        self.assertEqual(200, result.status_code)
        body = result.json()
        self.assertIn("assistant", body["targets"])
        self.assertIn("clipboard", body["targets"])
        self.assertIn("codex", body["targets"])
        self.assertIn("paste", body["targets"])
        self.assertIn("audio_capture", body["events"])
        self.assertIn("send_audio_to_assistant", body["actions"])
        self.assertIn("GET /status", body["endpoints"])
        self.assertIn("GET /commands/{id}", body["endpoints"])
        self.assertIn({"target": "paste", "text": "Voice keyboard text"}, body["command_examples"])
        self.assertTrue(body["approval_required"])

    def test_clipboard_and_paste_targets_are_valid_commands(self) -> None:
        bridge = MobileBridge(MobileBridgeConfig(require_approval=True), EventBus())
        client = TestClient(bridge._build_app())

        copy_result = client.post("/commands", json={"target": "clipboard", "text": "Copy this"})
        paste_result = client.post("/commands", json={"target": "paste", "text": "Paste this"})

        self.assertEqual(200, copy_result.status_code)
        self.assertEqual(200, paste_result.status_code)
        self.assertEqual(["clipboard", "paste"], [item["target"] for item in bridge.pending])

    def test_bridge_accepts_phone_web_preflight_with_token_header(self) -> None:
        bridge = MobileBridge(MobileBridgeConfig(host="0.0.0.0", token="secret"), EventBus())
        client = TestClient(bridge._build_app())

        result = client.options(
            "/commands",
            headers={
                "Origin": "https://phone.local",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-wizpr-token, content-type",
            },
        )

        self.assertEqual(200, result.status_code)
        self.assertEqual("*", result.headers["access-control-allow-origin"])
        self.assertIn("POST", result.headers["access-control-allow-methods"])
        self.assertIn("X-Wizpr-Token", result.headers["access-control-allow-headers"])

    def test_token_required_for_events_when_bound_publicly(self) -> None:
        bridge = MobileBridge(MobileBridgeConfig(host="0.0.0.0", token="secret"), EventBus())
        client = TestClient(bridge._build_app())

        self.assertEqual(401, client.get("/events").status_code)
        self.assertEqual(200, client.get("/events", headers={"X-Wizpr-Token": "secret"}).status_code)

    def test_token_required_for_public_bridge_commands(self) -> None:
        bridge = MobileBridge(MobileBridgeConfig(host="0.0.0.0", token="secret"), EventBus())
        client = TestClient(bridge._build_app())

        missing = client.post("/commands", json={"target": "assistant", "text": "Hello"})
        ok = client.post(
            "/commands",
            headers={"X-Wizpr-Token": "secret"},
            json={"target": "assistant", "text": "Hello"},
        )

        self.assertEqual(401, missing.status_code)
        self.assertEqual(200, ok.status_code)


if __name__ == "__main__":
    unittest.main()
