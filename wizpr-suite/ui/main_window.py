from __future__ import annotations

import asyncio
import html
import inspect
import math
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.config import (
    AppConfig,
    BUTTON_MODE_MAPPINGS,
    BUTTON_TOPICS,
    _sync_button_mode_mappings,
    load_config,
    save_config,
)
from ..core.codex_bridge import detect_codex_executable, run_codex_prompt
from ..core.desktop_tools import DesktopToolRequest, execute_desktop_tool, parse_desktop_tool_request
from ..core.local_transcription import (
    audio_preflight_reason,
    clean_transcript,
    close_local_transcriber,
    local_transcription_request_timeout_seconds,
    local_transcription_uses_persistent_worker,
    transcribe_audio_local,
    transcript_rejection_reason,
    warm_local_transcriber,
)
from ..core.memory import PersistentMemory
from ..core.mobile_bridge import MobileBridge, bridge_app_url, bridge_needs_token, bridge_url, make_bridge_token
from ..core.opencode_bridge import detect_opencode_executable, list_opencode_models, run_opencode_prompt
from ..core.speech import play_feedback_sound, speak_text
from ..core.logging_setup import get_logger
from ..core.event_bus import EventBus
from ..core.action_router import ActionRouter
from ..ble.ble_manager import BLEManager, DiscoveredDevice, WIZPR_RING_SERVICE_UUID
from ..ble.ring_controller import RingController, RingProfile
from ..llm.registry import ProviderRegistry
from ..llm.providers.openai_provider import OpenAIProvider
from ..llm.providers.ollama_provider import OllamaProvider, sort_ollama_models
from ..llm.providers.openai_compat_provider import OpenAICompatProvider

logger = get_logger("wizpr_suite.ui")
AUTO_VOICE_DUPLICATE_WINDOW_SECONDS = 1.25
VOICE_CAPTURE_ACTIONS = {
    "send_audio_to_assistant",
    "send_audio_to_codex",
    "send_audio_to_opencode",
    "transcribe_audio_only",
    "copy_audio_to_clipboard",
    "paste_audio_to_active_app",
}
OLLAMA_STATUS_DISCOVERY_TIMEOUT_SECONDS = 0.75
OLLAMA_STATUS_MODEL_TIMEOUT_SECONDS = 1.75
RING_LOW_BATTERY_PERCENT = 15
RING_LOW_BATTERY_CLEAR_PERCENT = 20
SAVED_RING_STARTUP_SCAN_SECONDS = 75.0
SAVED_RING_RETRY_SCAN_SECONDS = 45.0
SAVED_RING_RETRY_DELAY_SECONDS = 5.0
CODING_VOICE_REVIEW_ACTION_RE = re.compile(
    r"^(?:please\s+)?(?:"
    r"open|launch|start|run|close|quit|exit|switch(?:\s+to)?|click|press|type|paste|copy|"
    r"send\s+(?:key|keys|hotkey)|alt\s+tab|control\s+v|ctrl\s+v|"
    r"show\s+(?:me\s+)?(?:notepad|calculator|browser|chrome|edge|powershell|cmd|terminal)"
    r")\b",
    re.IGNORECASE,
)
CODING_VOICE_REVIEW_APP_RE = re.compile(
    r"\b(?:notepad|calculator|chrome|edge|browser|powershell|cmd|terminal|desktop|window|active\s+app)\b",
    re.IGNORECASE,
)


class QtLogEmitter(QtCore.QObject):
    line = QtCore.Signal(str)


class QtLogHandler(logging.Handler):
    def __init__(self, emitter: QtLogEmitter) -> None:
        super().__init__()
        self.emitter = emitter
        self.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        self.emitter.line.emit(msg)


class AsyncBridge(QtCore.QObject):
    tick = QtCore.Signal()

    def __init__(self, loop: asyncio.AbstractEventLoop, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self.loop = loop
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(5)
        self.timer.timeout.connect(self._on_timeout)

    def start(self) -> None:
        self.timer.start()

    def stop(self) -> None:
        try:
            self.timer.stop()
        except Exception:
            pass

    def _on_timeout(self) -> None:
        if getattr(self.loop, "is_closed", lambda: False)():
            self.stop()
            return
        self.tick.emit()
        try:
            self.loop.call_soon(self.loop.stop)
            self.loop.run_forever()
        except Exception:
            logger.exception("Async loop tick failed")


class VoiceWaveformWidget(QtWidgets.QWidget):
    """Small animated waveform used by the conversation surface."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(38)
        self.setMaximumHeight(48)
        self._active = False
        self._phase = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(42)
        self._timer.timeout.connect(self._advance)

    def set_active(self, active: bool) -> None:
        active = bool(active)
        if self._active == active:
            return
        self._active = active
        if active:
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _advance(self) -> None:
        self._phase += 0.32
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        del event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        rect = self.rect().adjusted(4, 5, -4, -5)
        if rect.width() <= 4 or rect.height() <= 4:
            return
        bar_count = max(20, min(48, rect.width() // 8))
        gap = 3.0
        bar_width = max(2.0, (rect.width() - gap * (bar_count - 1)) / bar_count)
        center = rect.center().y()
        color = QtGui.QColor('#169bff' if self._active else '#17598e')
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(color)
        for index in range(bar_count):
            wave = abs(math.sin(self._phase + index * 0.47))
            carrier = 0.35 + 0.65 * abs(math.sin(index * 0.21 + 1.1))
            strength = wave * carrier if self._active else 0.12 + 0.14 * carrier
            height = max(3.0, rect.height() * strength)
            x = rect.left() + index * (bar_width + gap)
            painter.drawRoundedRect(QtCore.QRectF(x, center - height / 2, bar_width, height), 1.5, 1.5)


class ConversationView(QtWidgets.QTextBrowser):
    """Styled conversation view with throttled rendering for low-latency BLE audio."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setOpenExternalLinks(False)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.document().setDocumentMargin(8)
        self._plain_text = ""
        self._messages: list[dict[str, str]] = []
        self._max_blocks = 3000
        self._compat_line_wrap_mode = QtWidgets.QPlainTextEdit.WidgetWidth
        self._render_timer = QtCore.QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(90)
        self._render_timer.timeout.connect(self._render)
        self._render()

    def setMaximumBlockCount(self, count: int) -> None:
        self._max_blocks = max(100, int(count))
        self.document().setMaximumBlockCount(self._max_blocks)

    def setLineWrapMode(self, mode: object) -> None:
        self._compat_line_wrap_mode = mode
        self.setWordWrapMode(QtGui.QTextOption.WrapAtWordBoundaryOrAnywhere)

    def lineWrapMode(self) -> object:
        return self._compat_line_wrap_mode

    def clear(self) -> None:
        self._render_timer.stop()
        self._plain_text = ""
        self._messages.clear()
        super().clear()
        self._render()

    def setPlainText(self, text: str) -> None:
        self._render_timer.stop()
        self._plain_text = str(text or "")
        self._messages = []
        if self._plain_text.strip():
            self._messages.append({"role": "assistant", "text": self._plain_text.strip()})
        self._render()

    def toPlainText(self) -> str:
        return self._plain_text

    def appendPlainText(self, text: str) -> None:
        value = str(text or "")
        if not value:
            return
        self._plain_text = f"{self._plain_text}\n{value}" if self._plain_text else value
        stripped = value.strip()
        if not stripped:
            return
        prompt_match = re.match(r"^>\s*\[[^\]]+\]\s*(.*)$", stripped, re.DOTALL)
        if prompt_match:
            role = "user"
            display = prompt_match.group(1).strip()
        elif stripped.startswith("["):
            role = "system"
            display = stripped
        else:
            role = "assistant"
            display = stripped
        self._messages.append({"role": role, "text": display})
        self._schedule_render(immediate=True)

    def append_stream_text(self, text: str) -> None:
        value = str(text or "")
        if not value:
            return
        self._plain_text += value
        if self._messages and self._messages[-1]["role"] == "assistant":
            self._messages[-1]["text"] += value
        else:
            self._messages.append({"role": "assistant", "text": value})
        self._schedule_render(immediate=False)

    def _schedule_render(self, *, immediate: bool) -> None:
        if immediate:
            self._render_timer.stop()
            self._render()
            return
        if not self._render_timer.isActive():
            self._render_timer.start()

    def _message_html(role: str, text: str) -> str:
        safe = html.escape(text).replace("\n", "<br>")
        if role == "user":
            return (
                '<table width="100%" cellspacing="0" cellpadding="0" style="margin:8px 0;">'
                '<tr><td width="29%"></td><td bgcolor="#18212c" style="border:1px solid #303b49; '
                'padding:12px 14px; border-radius:12px;"><span style="color:#8795a8; font-size:9pt;">You</span>'
                f'<br><span style="color:#f2f7ff; font-size:11pt;">{safe}</span></td></tr></table>'
            )
        if role == "system":
            return (
                '<table width="100%" cellspacing="0" cellpadding="0" style="margin:5px 0;">'
                '<tr><td bgcolor="#0d2235" style="border:1px solid #174d75; padding:8px 12px;">'
                f'<span style="color:#83c9ff; font-size:9.5pt;">{safe}</span></td></tr></table>'
            )
        return (
            '<table width="100%" cellspacing="0" cellpadding="0" style="margin:8px 0;">'
            '<tr><td bgcolor="#151b22" style="border:1px solid #303843; padding:13px 15px;">'
            '<span style="color:#2ba8ff; font-size:9pt; font-weight:600;">Wizpr</span>'
            f'<br><span style="color:#edf4fc; font-size:11pt;">{safe}</span></td><td width="17%"></td></tr></table>'
        )

    def _render(self) -> None:
        body = "".join(self._message_html(item["role"], item["text"]) for item in self._messages[-120:])
        if not body:
            body = (
                '<div style="color:#65758a; text-align:center; padding:72px 20px; font-size:11pt;">'
                'Your conversation will appear here.<br><span style="color:#2c9cff;">Press the ring button to begin.</span>'
                '</div>'
            )
        self.setHtml(f'<html><body style="background:#0b1016; margin:0;">{body}</body></html>')
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, app_dir: Path) -> None:
        super().__init__()
        self.setWindowTitle("Wizpr Suite 2.0")
        self.resize(1100, 720)
        self.setMinimumSize(560, 420)

        self.app_dir = app_dir
        self.cfg: AppConfig = load_config(app_dir)
        self.memory = PersistentMemory(app_dir)

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.bridge = AsyncBridge(self.loop, self)
        self.bridge.start()

        self.bus = EventBus()
        self.router = ActionRouter()
        self.mobile_bridge = MobileBridge(
            self.cfg.mobile_bridge,
            self.bus,
            self._handle_mobile_bridge_command,
            self._mobile_bridge_status_payload,
        )
        self._bridge_pending_by_id: dict[str, dict[str, Any]] = {}
        self.ble = BLEManager()
        self.ble.on_disconnect = self._on_ble_disconnected
        self.ring_profile = RingProfile(address=self.cfg.last_ble_address)
        self.ring = RingController(
            self.ble,
            self.bus,
            self.ring_profile,
            capture_dir=self.app_dir / "captures",
            audio_finalize_delay=max(0, int(self.cfg.transcription.ring_audio_finalize_delay_ms)) / 1000.0,
            audio_idle_finalize_delay=max(0, int(self.cfg.transcription.ring_audio_idle_finalize_delay_ms)) / 1000.0,
        )

        self.registry = ProviderRegistry()
        self._init_providers()

        self._listen_enabled = False
        self._voice_ui_active = False
        self._ring_connection_text = "Disconnected"
        self._ring_connection_state = "disconnected"
        self._last_transcript = ""
        self._last_audio_path: Path | None = None
        self._ring_keep_connected = False
        self._ring_manual_disconnect = False
        self._ring_connecting = False
        self._ring_reconnect_task: asyncio.Task[None] | None = None
        self._ring_keepalive_task: asyncio.Task[None] | None = None
        self._ring_sleep_timeout_task: asyncio.Task[None] | None = None
        self._startup_auto_connect_task: asyncio.Task[None] | None = None
        self._speech_queue: list[tuple[int, str]] = []
        self._speech_task: asyncio.Task[None] | None = None
        self._last_response_text = ""
        self._active_llm_status_task: asyncio.Task[None] | None = None
        self._ollama_warm_task: asyncio.Task[None] | None = None
        self._warm_task: asyncio.Task[None] | None = None
        self._ble_log_backlog: list[str] = []
        self._last_auto_voice_signature: tuple[str, str] | None = None
        self._last_auto_voice_at = 0.0
        self._audio_transcript_cache: dict[tuple[Any, ...], str] = {}
        self._assistant_tasks: set[asyncio.Task[Any]] = set()
        self._voice_transcription_lock = asyncio.Lock()
        self._voice_turn = 0
        self._voice_capture_generation = 0
        self._voice_session_id: int | None = None
        self._voice_capture_active = False
        self._voice_pipeline_task: asyncio.Task[Any] | None = None
        self._voice_interrupt_probe_task: asyncio.Task[Any] | None = None
        self._voice_capture_started_at = 0.0
        self._response_generation = 0
        self._active_response_count = 0
        self._speech_generation = 0
        self._ring_low_battery_active = False
        self._ring_low_battery_level: int | None = None
        self._pending_tool_request: DesktopToolRequest | None = None
        self._capture_waiting_for_interrupt_word = False

        self._setup_logging_panel()
        self._build_ui()
        self._apply_theme()

        self._register_actions()
        self.loop.create_task(self._wire_bus())
        self._schedule_voice_warmup_at_startup()
        QtCore.QTimer.singleShot(800, self._schedule_active_llm_status_check)
        QtCore.QTimer.singleShot(1200, self._schedule_saved_ring_auto_connect)
        if self.cfg.mobile_bridge.enabled:
            self.loop.create_task(self._start_mobile_bridge(show_status=False))


    def _init_providers(self) -> None:
        self.p_openai = OpenAIProvider(api_key=self.cfg.openai.api_key, base_url=self.cfg.openai.base_url)
        self.p_ollama = OllamaProvider(base_url=self.cfg.ollama.base_url)
        self.p_compat = OpenAICompatProvider(base_url=self.cfg.openai_compat.base_url, api_key=self.cfg.openai_compat.api_key)

        self.registry.register(self.p_openai)
        self.registry.register(self.p_ollama)
        self.registry.register(self.p_compat)

        ids = set(self.registry.list_ids())
        self.active_llm_id = self.cfg.active_llm_id if self.cfg.active_llm_id in ids else "openai"
        self.cfg.active_llm_id = self.active_llm_id


    def _setup_logging_panel(self) -> None:
        self.log_emitter = QtLogEmitter()
        self.log_emitter.line.connect(self._append_log_line)

        self.log_box = QtWidgets.QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(4000)

        handler = QtLogHandler(self.log_emitter)
        handler.setLevel(logging.INFO)
        base_logger = logging.getLogger("wizpr_suite")
        base_logger.addHandler(handler)

    def _append_log_line(self, line: str) -> None:
        self.log_box.appendPlainText(line)


    def _build_ui(self) -> None:
        self.resize(1240, 790)
        self.setMinimumSize(900, 620)

        central = QtWidgets.QWidget()
        central.setObjectName('appRoot')
        shell = QtWidgets.QHBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        sidebar = QtWidgets.QFrame()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(205)
        side = QtWidgets.QVBoxLayout(sidebar)
        side.setContentsMargins(14, 14, 14, 14)
        side.setSpacing(10)

        brand = QtWidgets.QHBoxLayout()
        brand.setSpacing(9)
        brand_icon = QtWidgets.QLabel()
        brand_icon.setObjectName('brandIcon')
        logo_path = self._resource_path('wizpr_suite_logo.png')
        pixmap = QtGui.QPixmap(str(logo_path))
        brand_icon.setPixmap(pixmap.scaled(30, 30, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))
        brand_icon.setFixedSize(32, 32)
        brand.addWidget(brand_icon)
        brand_text = QtWidgets.QLabel('Wizpr Suite')
        brand_text.setObjectName('brandText')
        brand.addWidget(brand_text)
        brand.addStretch(1)
        side.addLayout(brand)

        ring_card = QtWidgets.QFrame()
        ring_card.setObjectName('ringCard')
        ring_lay = QtWidgets.QVBoxLayout(ring_card)
        ring_lay.setContentsMargins(10, 10, 10, 11)
        ring_lay.setSpacing(4)
        self.sidebar_ring_image = QtWidgets.QLabel()
        self.sidebar_ring_image.setAlignment(QtCore.Qt.AlignCenter)
        ring_pix = QtGui.QPixmap(str(self._resource_path('wizpr_ring_card.png')))
        self.sidebar_ring_image.setPixmap(ring_pix.scaled(145, 95, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))
        self.sidebar_ring_image.setMinimumHeight(92)
        ring_lay.addWidget(self.sidebar_ring_image)
        self.sidebar_ring_status = QtWidgets.QLabel('Ring Disconnected')
        self.sidebar_ring_status.setObjectName('sidebarRingStatus')
        self.sidebar_ring_status.setAlignment(QtCore.Qt.AlignCenter)
        ring_lay.addWidget(self.sidebar_ring_status)
        self.sidebar_battery_status = QtWidgets.QLabel('Battery --')
        self.sidebar_battery_status.setObjectName('sidebarBatteryStatus')
        self.sidebar_battery_status.setAlignment(QtCore.Qt.AlignCenter)
        ring_lay.addWidget(self.sidebar_battery_status)
        side.addWidget(ring_card)

        self.sidebar_buttons: dict[str, QtWidgets.QPushButton] = {}
        nav_items = [
            ('chat', '◉  Chat'),
            ('memory', '◆  Memory'),
            ('tools', '✚  Tools'),
            ('ring', '○  Ring'),
            ('voice', '◍  Voice'),
            ('appearance', '✦  Appearance'),
            ('advanced', '⚙  Advanced'),
            ('about', 'ⓘ  About'),
        ]
        for key, label in nav_items:
            button = QtWidgets.QPushButton(label)
            button.setObjectName('navButton')
            button.setCheckable(key in {'chat', 'ring'})
            button.setCursor(QtCore.Qt.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, section=key: self._navigate_primary(section))
            side.addWidget(button)
            self.sidebar_buttons[key] = button
        side.addStretch(1)

        self.quick_ready_label = QtWidgets.QLabel()
        self.quick_ready_label.setObjectName('sidebarSummary')
        self.quick_ready_label.setWordWrap(True)
        self.quick_ready_label.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        side.addWidget(self.quick_ready_label)
        version_label = QtWidgets.QLabel('Wizpr Suite  2.0')
        version_label.setObjectName('versionLabel')
        side.addWidget(version_label)
        shell.addWidget(sidebar)

        main_surface = QtWidgets.QFrame()
        main_surface.setObjectName('mainSurface')
        main = QtWidgets.QVBoxLayout(main_surface)
        main.setContentsMargins(18, 14, 18, 14)
        main.setSpacing(10)

        header = QtWidgets.QFrame()
        header.setObjectName('appHeader')
        header_lay = QtWidgets.QHBoxLayout(header)
        header_lay.setContentsMargins(12, 8, 10, 8)
        header_lay.setSpacing(11)

        self.header_logo = QtWidgets.QLabel()
        self.header_logo.setObjectName('headerLogo')
        self.header_logo.setPixmap(pixmap.scaled(54, 54, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))
        self.header_logo.setFixedSize(56, 56)
        header_lay.addWidget(self.header_logo)

        title_box = QtWidgets.QVBoxLayout()
        title_box.setSpacing(1)
        title = QtWidgets.QLabel('Wizpr')
        title.setObjectName('headerTitle')
        subtitle = QtWidgets.QLabel('At your service.')
        subtitle.setObjectName('headerSubtitle')
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header_lay.addLayout(title_box)
        header_lay.addStretch(1)

        provider_box = QtWidgets.QVBoxLayout()
        provider_box.setSpacing(2)
        provider_label = QtWidgets.QLabel('ACTIVE ASSISTANT')
        provider_label.setObjectName('microLabel')
        provider_box.addWidget(provider_label)
        self.active_llm_combo = QtWidgets.QComboBox()
        self.active_llm_combo.setObjectName('providerCombo')
        for pid in self.registry.list_ids():
            self.active_llm_combo.addItem(self._provider_label(pid), pid)
        self._set_active_llm_combo(self.active_llm_id)
        self.active_llm_combo.currentIndexChanged.connect(lambda _idx: self._active_llm_combo_changed())
        self.active_llm_combo.setMinimumContentsLength(16)
        self.active_llm_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        provider_box.addWidget(self.active_llm_combo)
        header_lay.addLayout(provider_box)

        self.provider_setup_btn = QtWidgets.QPushButton('⚙')
        self.provider_setup_btn.setObjectName('headerToolButton')
        self.provider_setup_btn.setToolTip('Open provider settings.')
        self.provider_setup_btn.clicked.connect(self._show_provider_settings)
        header_lay.addWidget(self.provider_setup_btn)

        self.header_settings_btn = QtWidgets.QPushButton('☷')
        self.header_settings_btn.setObjectName('headerToolButton')
        self.header_settings_btn.setToolTip('Open Wizpr Suite settings.')
        self.header_settings_btn.clicked.connect(lambda: self._open_settings('general'))
        header_lay.addWidget(self.header_settings_btn)
        main.addWidget(header)

        self.active_llm_detail = QtWidgets.QLabel()
        self.active_llm_detail.setObjectName('statusStrip')
        self.active_llm_detail.setWordWrap(True)
        self.active_llm_detail.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        self.active_llm_detail.setText(self._active_llm_detail_text())
        settings_summary = getattr(self, "settings_provider_summary", None)
        if settings_summary is not None:
            settings_summary.setText(self._active_llm_detail_text())
        main.addWidget(self.active_llm_detail)

        self.next_step_label = QtWidgets.QLabel()
        self.next_step_label.setObjectName('nextStepStrip')
        self.next_step_label.setWordWrap(True)
        self.next_step_label.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        self.next_step_label.setText(self._next_step_text())
        main.addWidget(self.next_step_label)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setObjectName('workspaceTabs')
        self.tabs.setUsesScrollButtons(True)
        self.tabs.tabBar().hide()
        self.tabs.currentChanged.connect(self._workspace_changed)
        main.addWidget(self.tabs, 1)
        shell.addWidget(main_surface, 1)

        self.llm_tab_index = -1
        self.bridge_tab_index = -1
        self.mappings_tab_index = -1
        self.logs_tab_index = -1
        self._advanced_tabs_built = False
        self._advanced_ble_built = False

        self._build_settings_dialog()
        self._build_tab_ble()
        self._build_tab_chat()
        if self.cfg.show_advanced_options:
            self._ensure_advanced_tabs_built()
        self._set_advanced_visible(bool(self.cfg.show_advanced_options))
        self.tabs.setCurrentIndex(self.chat_tab_index)
        self._workspace_changed(self.chat_tab_index)
        self._refresh_quick_ready()

        self.setCentralWidget(central)
        self.statusBar().setObjectName('mainStatusBar')
        self.statusBar().showMessage('Ready', 1500)

    def _resource_path(self, name: str) -> Path:
        return Path(__file__).resolve().parents[1] / 'resources' / name

    def _workspace_changed(self, index: int) -> None:
        chat = getattr(self, 'chat_tab_index', -1)
        ring = getattr(self, 'ring_tab_index', -1)
        for key, button in getattr(self, 'sidebar_buttons', {}).items():
            if not button.isCheckable():
                continue
            checked = (key == 'chat' and index == chat) or (key == 'ring' and index == ring)
            button.blockSignals(True)
            try:
                button.setChecked(checked)
            finally:
                button.blockSignals(False)

    def _navigate_primary(self, section: str) -> None:
        if section == 'chat':
            self.tabs.setCurrentIndex(self.chat_tab_index)
            return
        if section == 'ring':
            self.tabs.setCurrentIndex(self.ring_tab_index)
            return
        if section in {'memory', 'tools', 'voice', 'appearance', 'advanced'}:
            self._open_settings(section)
            return
        if section == 'about':
            self._open_settings('about')

    def _open_settings(self, section: str = 'general') -> None:
        dialog = getattr(self, 'settings_dialog', None)
        if dialog is None:
            return
        row = self._settings_rows.get(section, 0)
        self.settings_nav.setCurrentRow(row)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _settings_page(self, title: str, subtitle: str) -> tuple[QtWidgets.QWidget, QtWidgets.QVBoxLayout]:
        page = QtWidgets.QWidget()
        page.setObjectName('settingsPage')
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 22)
        layout.setSpacing(12)
        heading = QtWidgets.QLabel(title)
        heading.setObjectName('settingsTitle')
        layout.addWidget(heading)
        description = QtWidgets.QLabel(subtitle)
        description.setObjectName('settingsSubtitle')
        description.setWordWrap(True)
        layout.addWidget(description)
        return page, layout

    def _settings_card(self, title: str) -> tuple[QtWidgets.QFrame, QtWidgets.QVBoxLayout]:
        card = QtWidgets.QFrame()
        card.setObjectName('settingsCard')
        layout = QtWidgets.QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        label = QtWidgets.QLabel(title)
        label.setObjectName('cardTitle')
        layout.addWidget(label)
        return card, layout

    def _build_settings_dialog(self) -> None:
        self.settings_dialog = QtWidgets.QDialog(self)
        self.settings_dialog.setObjectName('settingsDialog')
        self.settings_dialog.setWindowTitle('Wizpr Suite Settings')
        self.settings_dialog.resize(900, 610)
        self.settings_dialog.setMinimumSize(760, 520)
        outer = QtWidgets.QHBoxLayout(self.settings_dialog)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.settings_nav = QtWidgets.QListWidget()
        self.settings_nav.setObjectName('settingsNav')
        self.settings_nav.setFixedWidth(190)
        sections = [
            ('general', '⚙  General'),
            ('voice', '◍  Voice & Speech'),
            ('memory', '◆  Memory'),
            ('behavior', '✧  AI Behavior'),
            ('tools', '✚  Tools'),
            ('privacy', '◎  Privacy'),
            ('appearance', '✦  Appearance'),
            ('advanced', '⚒  Advanced'),
            ('about', 'ⓘ  About'),
        ]
        self._settings_rows = {key: idx for idx, (key, _label) in enumerate(sections)}
        for _key, label in sections:
            self.settings_nav.addItem(label)
        outer.addWidget(self.settings_nav)

        self.settings_stack = QtWidgets.QStackedWidget()
        self.settings_stack.setObjectName('settingsStack')
        outer.addWidget(self.settings_stack, 1)
        self.settings_nav.currentRowChanged.connect(self.settings_stack.setCurrentIndex)

        general, lay = self._settings_page('General', 'Core assistant and application controls.')
        card, card_lay = self._settings_card('Assistant provider')
        self.settings_provider_summary = QtWidgets.QLabel(self._active_llm_detail_text())
        self.settings_provider_summary.setWordWrap(True)
        card_lay.addWidget(self.settings_provider_summary)
        provider_button = QtWidgets.QPushButton('Open Provider Setup')
        provider_button.setObjectName('primaryButton')
        provider_button.clicked.connect(self._show_provider_settings)
        card_lay.addWidget(provider_button)
        lay.addWidget(card)
        lay.addStretch(1)
        self.settings_stack.addWidget(general)

        voice, lay = self._settings_page('Voice & Speech', 'Control ring routing, spoken responses, and how interruptions work.')
        interrupt_card, interrupt_lay = self._settings_card('Interrupt behavior')
        mode_row = QtWidgets.QHBoxLayout()
        mode_row.addWidget(QtWidgets.QLabel('Mode'))
        self.interrupt_mode_combo = QtWidgets.QComboBox()
        self.interrupt_mode_combo.addItem('Interrupt phrase (recommended)', 'word')
        self.interrupt_mode_combo.addItem('Ring activity', 'ring')
        self.interrupt_mode_combo.addItem('Phrase or ring', 'both')
        self.interrupt_mode_combo.addItem('Disabled', 'off')
        self._set_combo_data(self.interrupt_mode_combo, self.cfg.transcription.interrupt_mode, 'word')
        self.interrupt_mode_combo.currentIndexChanged.connect(lambda _idx: self._interrupt_settings_changed())
        mode_row.addWidget(self.interrupt_mode_combo, 1)
        interrupt_lay.addLayout(mode_row)
        phrase_row = QtWidgets.QHBoxLayout()
        phrase_row.addWidget(QtWidgets.QLabel('Interrupt phrase'))
        self.interrupt_word_edit = QtWidgets.QLineEdit(self.cfg.transcription.interrupt_word or 'stop')
        self.interrupt_word_edit.setPlaceholderText('stop')
        self.interrupt_word_edit.editingFinished.connect(self._interrupt_settings_changed)
        phrase_row.addWidget(self.interrupt_word_edit, 1)
        interrupt_lay.addLayout(phrase_row)
        lay.addWidget(interrupt_card)

        routing_card, routing_lay = self._settings_card('Ring conversation')
        target_row = QtWidgets.QHBoxLayout()
        target_row.addWidget(QtWidgets.QLabel('Voice target'))
        self.ring_voice_target = QtWidgets.QComboBox()
        self.ring_voice_target.addItem('Assistant', 'assistant')
        self.ring_voice_target.addItem('Codex', 'codex')
        self.ring_voice_target.addItem('OpenCode', 'opencode')
        self.ring_voice_target.addItem('Transcript Only', 'transcript')
        self.ring_voice_target.addItem('Copy Text', 'clipboard')
        self.ring_voice_target.addItem('Voice Keyboard', 'paste')
        self._sync_ring_voice_target_ui()
        self.ring_voice_target.currentIndexChanged.connect(lambda _idx: self._ring_voice_target_changed())
        target_row.addWidget(self.ring_voice_target, 1)
        routing_lay.addLayout(target_row)
        self.speak_responses_check = QtWidgets.QCheckBox('Speak assistant responses')
        self.speak_responses_check.setChecked(bool(self.cfg.transcription.speak_responses))
        self.speak_responses_check.toggled.connect(self._speak_responses_changed)
        routing_lay.addWidget(self.speak_responses_check)
        self.wake_required_check = QtWidgets.QCheckBox('Require a wake phrase before sending')
        self.wake_required_check.setChecked(bool(self.cfg.transcription.require_wake_word))
        self.wake_required_check.toggled.connect(self._wake_required_changed)
        routing_lay.addWidget(self.wake_required_check)
        wake_row = QtWidgets.QHBoxLayout()
        self.wake_phrase_label = QtWidgets.QLabel('Wake phrase')
        wake_row.addWidget(self.wake_phrase_label)
        self.wake_phrase_edit = QtWidgets.QLineEdit()
        self.wake_phrase_edit.editingFinished.connect(self._simple_wake_phrase_changed)
        wake_row.addWidget(self.wake_phrase_edit, 1)
        routing_lay.addLayout(wake_row)
        self._sync_wake_phrase_ui()
        self._sync_wake_required_ui()
        controls = QtWidgets.QHBoxLayout()
        self.stop_speech_btn = QtWidgets.QPushButton('Stop Speech')
        self.stop_speech_btn.clicked.connect(self._stop_speech)
        controls.addWidget(self.stop_speech_btn)
        self.replay_response_btn = QtWidgets.QPushButton('Replay Last Response')
        self.replay_response_btn.clicked.connect(self._replay_last_response)
        self.replay_response_btn.setEnabled(False)
        controls.addWidget(self.replay_response_btn)
        routing_lay.addLayout(controls)
        lay.addWidget(routing_card)
        self.voice_status_label = QtWidgets.QLabel(self._wake_required_status_text())
        self.voice_status_label.setObjectName('settingsStatus')
        self.voice_status_label.setWordWrap(True)
        lay.addWidget(self.voice_status_label)
        lay.addStretch(1)
        self.settings_stack.addWidget(voice)

        memory, lay = self._settings_page('Memory', 'Choose whether Wizpr remembers recent conversations and explicit facts across restarts.')
        card, card_lay = self._settings_card('Persistent local memory')
        self.memory_enabled_check = QtWidgets.QCheckBox('Enable memory')
        self.memory_enabled_check.setChecked(bool(self.cfg.memory.enabled))
        self.memory_enabled_check.toggled.connect(self._memory_enabled_changed)
        card_lay.addWidget(self.memory_enabled_check)
        memory_note = QtWidgets.QLabel('Memory is stored locally in your Wizpr Suite application data folder.')
        memory_note.setWordWrap(True)
        card_lay.addWidget(memory_note)
        self.memory_manage_btn = QtWidgets.QPushButton('Manage Memory')
        self.memory_manage_btn.setObjectName('primaryButton')
        self.memory_manage_btn.clicked.connect(self._show_memory_dialog)
        card_lay.addWidget(self.memory_manage_btn)
        lay.addWidget(card)
        lay.addStretch(1)
        self.settings_stack.addWidget(memory)

        behavior, lay = self._settings_page('AI Behavior', 'Select the assistant backend and review its current model configuration.')
        card, card_lay = self._settings_card('Active assistant')
        behavior_summary = QtWidgets.QLabel(self._active_llm_detail_text())
        behavior_summary.setWordWrap(True)
        card_lay.addWidget(behavior_summary)
        behavior_provider = QtWidgets.QPushButton('Configure Models and Providers')
        behavior_provider.setObjectName('primaryButton')
        behavior_provider.clicked.connect(self._show_provider_settings)
        card_lay.addWidget(behavior_provider)
        lay.addWidget(card)
        lay.addStretch(1)
        self.settings_stack.addWidget(behavior)

        tools, lay = self._settings_page('Tools', 'Control whether the assistant may open approved desktop applications and folders.')
        card, card_lay = self._settings_card('Desktop tool access')
        tool_row = QtWidgets.QHBoxLayout()
        tool_row.addWidget(QtWidgets.QLabel('Permission'))
        self.tool_permission_combo = QtWidgets.QComboBox()
        self.tool_permission_combo.addItem('Disabled', 'off')
        self.tool_permission_combo.addItem('Ask every time', 'ask')
        self.tool_permission_combo.addItem('Auto-allow safe tools', 'allow')
        self._set_combo_data(self.tool_permission_combo, self.cfg.tools.permission_mode, 'ask')
        self.tool_permission_combo.currentIndexChanged.connect(lambda _idx: self._tool_permission_changed())
        tool_row.addWidget(self.tool_permission_combo, 1)
        card_lay.addLayout(tool_row)
        tool_note = QtWidgets.QLabel('Only the built-in allowlist is available. Arbitrary shell commands are not executed.')
        tool_note.setWordWrap(True)
        card_lay.addWidget(tool_note)
        self.run_tool_btn = QtWidgets.QPushButton('Run Pending Tool')
        self.run_tool_btn.setObjectName('primaryButton')
        self.run_tool_btn.setEnabled(False)
        self.run_tool_btn.clicked.connect(self._run_pending_tool)
        card_lay.addWidget(self.run_tool_btn)
        lay.addWidget(card)
        lay.addStretch(1)
        self.settings_stack.addWidget(tools)

        privacy, lay = self._settings_page('Privacy', 'Wizpr keeps local settings and memory on this computer unless you select a cloud provider.')
        card, card_lay = self._settings_card('Privacy controls')
        privacy_text = QtWidgets.QLabel(
            '• Memory is local and can be disabled or cleared.\n'
            '• Desktop tools are controlled by the Tools permission setting.\n'
            '• Ring captures are used for transcription and are not included in release builds.\n'
            '• Cloud requests are sent only to the provider you configure.'
        )
        privacy_text.setWordWrap(True)
        card_lay.addWidget(privacy_text)
        lay.addWidget(card)
        lay.addStretch(1)
        self.settings_stack.addWidget(privacy)

        appearance, lay = self._settings_page('Appearance', 'Choose the application theme and visual style.')
        card, card_lay = self._settings_card('Theme')
        self.theme_btn = QtWidgets.QPushButton('Toggle Light / Dark Theme')
        self.theme_btn.setObjectName('primaryButton')
        self.theme_btn.clicked.connect(self._toggle_theme)
        card_lay.addWidget(self.theme_btn)
        appearance_note = QtWidgets.QLabel('The reference-inspired midnight-blue layout is used in dark mode.')
        appearance_note.setWordWrap(True)
        card_lay.addWidget(appearance_note)
        lay.addWidget(card)
        lay.addStretch(1)
        self.settings_stack.addWidget(appearance)

        advanced, lay = self._settings_page('Advanced', 'Expose provider, bridge, mapping, diagnostics, and logging pages.')
        card, card_lay = self._settings_card('Advanced workspace')
        self.advanced_toggle = QtWidgets.QCheckBox('Enable advanced pages')
        self.advanced_toggle.setChecked(bool(self.cfg.show_advanced_options))
        self.advanced_toggle.toggled.connect(self._advanced_changed)
        card_lay.addWidget(self.advanced_toggle)
        open_provider = QtWidgets.QPushButton('Open Provider Workspace')
        open_provider.setObjectName('primaryButton')
        open_provider.clicked.connect(self._show_provider_settings)
        card_lay.addWidget(open_provider)
        lay.addWidget(card)
        lay.addStretch(1)
        self.settings_stack.addWidget(advanced)

        about, lay = self._settings_page('About', 'Wizpr Suite turns the ring into a fast, private, configurable AI interface.')
        card, card_lay = self._settings_card('Wizpr Suite')
        about_logo = QtWidgets.QLabel()
        about_logo.setAlignment(QtCore.Qt.AlignCenter)
        logo = QtGui.QPixmap(str(self._resource_path('wizpr_suite_logo.png')))
        about_logo.setPixmap(logo.scaled(150, 150, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))
        card_lay.addWidget(about_logo)
        about_text = QtWidgets.QLabel('Version 2.0\nPrivate. Powerful. Personal.')
        about_text.setAlignment(QtCore.Qt.AlignCenter)
        card_lay.addWidget(about_text)
        lay.addWidget(card)
        lay.addStretch(1)
        self.settings_stack.addWidget(about)

        self.settings_nav.setCurrentRow(0)


    def _scroll_tab(self, content: QtWidgets.QWidget) -> QtWidgets.QScrollArea:
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        content.setMinimumWidth(0)
        scroll.setWidget(content)
        return scroll

    def _button_grid_widget(self, buttons: list[QtWidgets.QPushButton], columns: int = 3) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        columns = max(1, columns)
        for idx, button in enumerate(buttons):
            button.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
            grid.addWidget(button, idx // columns, idx % columns)
        for col in range(columns):
            grid.setColumnStretch(col, 1)
        return widget

    def _settings_form(self, parent: QtWidgets.QWidget) -> QtWidgets.QFormLayout:
        form = QtWidgets.QFormLayout(parent)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.WrapAllRows)
        form.setLabelAlignment(QtCore.Qt.AlignLeft)
        form.setFormAlignment(QtCore.Qt.AlignTop)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        return form

    def _field_with_actions(self, field: QtWidgets.QWidget, buttons: list[QtWidgets.QPushButton]) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(widget)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        field.setMinimumWidth(0)
        field.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        lay.addWidget(field)
        lay.addWidget(self._button_grid_widget(buttons, columns=max(1, min(3, len(buttons)))))
        return widget

    def _ring_voice_mode_combo(self) -> QtWidgets.QComboBox:
        combo = QtWidgets.QComboBox()
        combo.addItem("Proximity Voice", "proximity")
        combo.addItem("All Voice", "all")
        combo.setMinimumContentsLength(14)
        combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        return combo

    def _ring_sleep_timeout_combo(self) -> QtWidgets.QComboBox:
        combo = QtWidgets.QComboBox()
        combo.addItem("5 seconds", 5)
        combo.addItem("3 seconds", 3)
        combo.addItem("10 seconds", 10)
        combo.addItem("Not in use", 0)
        combo.setMinimumContentsLength(10)
        combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        return combo

    def _ring_button_mode_combo(self) -> QtWidgets.QComboBox:
        combo = QtWidgets.QComboBox()
        combo.addItem("App Style", "app")
        combo.addItem("Coding", "coding")
        combo.addItem("Custom", "custom")
        combo.setMinimumContentsLength(10)
        combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        return combo

    def _build_tab_ble(self) -> None:
        tab = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(tab)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(10)

        self.wizpr_auto_btn = QtWidgets.QPushButton("Auto Connect Ring")
        self.wizpr_auto_btn.clicked.connect(self._auto_connect_wizpr)

        self.ble_disconnect_btn = QtWidgets.QPushButton("Disconnect")
        self.ble_disconnect_btn.clicked.connect(self._disconnect_ble)
        lay.addWidget(self._button_grid_widget([self.wizpr_auto_btn, self.ble_disconnect_btn], columns=2))

        self.ring_connection_status = QtWidgets.QLabel("Disconnected")
        self.ring_connection_status.setAlignment(QtCore.Qt.AlignCenter)
        if self.cfg.last_ble_address:
            self._set_ring_connection_status("Remembered", "neutral")
            self.ring_connection_status.setToolTip(self.cfg.last_ble_address)
        else:
            self._set_ring_connection_status("Disconnected", "disconnected")

        self.ring_battery_status = QtWidgets.QLabel("Battery: --")
        self.ring_battery_status.setWordWrap(True)

        self.ring_last_event = QtWidgets.QLabel("Last event: --")
        self.ring_last_event.setWordWrap(True)
        self.ring_last_event.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)

        self.ring_saved_status = QtWidgets.QLabel()
        self.ring_saved_status.setWordWrap(True)

        self.forget_ring_btn = QtWidgets.QPushButton("Forget Ring")
        self.forget_ring_btn.clicked.connect(self._forget_saved_ring)

        status_box = QtWidgets.QWidget()
        status_lay = QtWidgets.QVBoxLayout(status_box)
        status_lay.setContentsMargins(0, 0, 0, 0)
        status_lay.setSpacing(6)
        connection_lay = QtWidgets.QHBoxLayout()
        connection_lay.setContentsMargins(0, 0, 0, 0)
        connection_lay.setSpacing(8)
        connection_lay.addWidget(QtWidgets.QLabel("Ring:"))
        connection_lay.addWidget(self.ring_connection_status, 1)
        status_lay.addLayout(connection_lay)
        status_lay.addWidget(self.ring_battery_status)
        status_lay.addWidget(self.ring_last_event)
        status_lay.addWidget(self.ring_saved_status)
        status_lay.addWidget(self.forget_ring_btn)
        lay.addWidget(status_box)
        self._update_saved_ring_status()
        if self.cfg.last_ble_address:
            self.ring_last_event.setText("Last event: remembered ring")
        else:
            self.ring_last_event.setText("Last event: disconnected")

        settings_box = QtWidgets.QGroupBox("Ring Settings")
        settings = self._settings_form(settings_box)
        self.ring_voice_mode = self._ring_voice_mode_combo()
        settings.addRow("Voice Mode:", self.ring_voice_mode)

        self.ring_sleep_timeout = self._ring_sleep_timeout_combo()
        settings.addRow("Sleep Timeout:", self.ring_sleep_timeout)

        self.ring_button_mode = self._ring_button_mode_combo()
        settings.addRow("Button Mode:", self.ring_button_mode)

        self.ring_button_summary = QtWidgets.QLabel()
        self.ring_button_summary.setWordWrap(True)
        settings.addRow("", self.ring_button_summary)

        self.ring_tts_response_check = QtWidgets.QCheckBox("TTS voice response")
        settings.addRow("", self.ring_tts_response_check)

        self.ring_auto_start_check = QtWidgets.QCheckBox("Listen for saved ring when app opens")
        settings.addRow("", self.ring_auto_start_check)

        self.ring_connect_sound_check = QtWidgets.QCheckBox("Connect sound")
        settings.addRow("", self.ring_connect_sound_check)

        self.ring_mic_sound_check = QtWidgets.QCheckBox("Mic start/stop sound")
        settings.addRow("", self.ring_mic_sound_check)

        self.ring_low_battery_check = QtWidgets.QCheckBox("Low battery warning")
        settings.addRow("", self.ring_low_battery_check)

        self._sync_ring_settings_ui()
        self.ring_voice_mode.currentIndexChanged.connect(lambda _idx: self._simple_ring_settings_changed())
        self.ring_sleep_timeout.currentIndexChanged.connect(lambda _idx: self._simple_ring_settings_changed())
        self.ring_button_mode.currentIndexChanged.connect(lambda _idx: self._simple_ring_settings_changed())
        self.ring_tts_response_check.toggled.connect(lambda _checked: self._simple_ring_settings_changed())
        self.ring_auto_start_check.toggled.connect(lambda _checked: self._simple_ring_settings_changed())
        self.ring_connect_sound_check.toggled.connect(lambda _checked: self._simple_ring_settings_changed())
        self.ring_mic_sound_check.toggled.connect(lambda _checked: self._simple_ring_settings_changed())
        self.ring_low_battery_check.toggled.connect(lambda _checked: self._simple_ring_settings_changed())
        lay.addWidget(settings_box)

        lay.addWidget(QtWidgets.QLabel("Activity"))
        self.ring_activity_box = QtWidgets.QPlainTextEdit()
        self.ring_activity_box.setReadOnly(True)
        self.ring_activity_box.setMaximumBlockCount(250)
        self.ring_activity_box.setMinimumHeight(90)
        self.ring_activity_box.setMaximumHeight(150)
        lay.addWidget(self.ring_activity_box)
        if self.cfg.last_ble_address:
            self._append_ring_activity(f"Saved ring: {self.cfg.last_ble_address}")
        else:
            self._append_ring_activity("No saved ring.")

        self.advanced_ble_mount = QtWidgets.QWidget()
        self.advanced_ble_mount_lay = QtWidgets.QVBoxLayout(self.advanced_ble_mount)
        self.advanced_ble_mount_lay.setContentsMargins(0, 0, 0, 0)
        self.advanced_ble_mount_lay.setSpacing(10)
        lay.addWidget(self.advanced_ble_mount)

        self.ring_tab_index = self.tabs.addTab(self._scroll_tab(tab), "Ring")

    def _ensure_advanced_ble_built(self) -> None:
        if self._advanced_ble_built:
            return

        self.advanced_ble_box = QtWidgets.QGroupBox("Advanced BLE Tools")
        self.advanced_ble_box.setCheckable(True)
        self.advanced_ble_box.setChecked(False)
        advanced_outer = QtWidgets.QVBoxLayout(self.advanced_ble_box)
        self.advanced_ble_body = QtWidgets.QWidget()
        advanced = QtWidgets.QVBoxLayout(self.advanced_ble_body)
        advanced.setContentsMargins(0, 0, 0, 0)
        advanced.setSpacing(8)

        self.ble_scan_btn = QtWidgets.QPushButton("Scan All")
        self.ble_scan_btn.clicked.connect(self._scan_ble)

        self.ble_windows_btn = QtWidgets.QPushButton("Load Windows Devices")
        self.ble_windows_btn.clicked.connect(self._load_windows_ble_devices)

        self.ble_doctor_btn = QtWidgets.QPushButton("BLE Doctor")
        self.ble_doctor_btn.clicked.connect(self._ble_doctor)
        advanced.addWidget(self._button_grid_widget([self.ble_scan_btn, self.ble_windows_btn, self.ble_doctor_btn], columns=3))

        self.ble_scan_seconds = QtWidgets.QDoubleSpinBox()
        self.ble_scan_seconds.setRange(1.0, 120.0)
        self.ble_scan_seconds.setSingleStep(1.0)
        self.ble_scan_seconds.setValue(60.0)
        scan_seconds_row = QtWidgets.QFormLayout()
        scan_seconds_row.setContentsMargins(0, 0, 0, 0)
        scan_seconds_row.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        scan_seconds_row.addRow("Scan sec:", self.ble_scan_seconds)
        advanced.addLayout(scan_seconds_row)

        self.ble_manual_address = QtWidgets.QLineEdit()
        self.ble_manual_address.setPlaceholderText("BLE address")

        self.ble_use_address_btn = QtWidgets.QPushButton("Use Address")
        self.ble_use_address_btn.clicked.connect(self._use_manual_ble_address)
        advanced.addWidget(self._field_with_actions(self.ble_manual_address, [self.ble_use_address_btn]))

        self.ble_connect_btn = QtWidgets.QPushButton("Connect Selected")
        self.ble_connect_btn.clicked.connect(lambda: self._connect_selected_ble(pair=False))

        self.ble_pair_connect_btn = QtWidgets.QPushButton("Pair + Connect")
        self.ble_pair_connect_btn.clicked.connect(lambda: self._connect_selected_ble(pair=True))

        self.ble_forget_ring_btn = QtWidgets.QPushButton("Forget Saved Ring")
        self.ble_forget_ring_btn.clicked.connect(self._forget_saved_ring)
        advanced.addWidget(
            self._button_grid_widget(
                [self.ble_connect_btn, self.ble_pair_connect_btn, self.ble_forget_ring_btn],
                columns=3,
            )
        )

        self.wizpr_guided_btn = QtWidgets.QPushButton("Guided Search")
        self.wizpr_guided_btn.clicked.connect(self._guided_ring_search)

        self.wizpr_subscribe_btn = QtWidgets.QPushButton("Subscribe")
        self.wizpr_subscribe_btn.clicked.connect(self._subscribe_wizpr_channels)

        self.wizpr_battery_btn = QtWidgets.QPushButton("Battery")
        self.wizpr_battery_btn.clicked.connect(self._query_wizpr_battery)

        self.wizpr_proxy_btn = QtWidgets.QPushButton("Proximity Check")
        self.wizpr_proxy_btn.setToolTip("Ask the ring for its proximity/VIDLE reading with GET_PROXY.")
        self.wizpr_proxy_btn.clicked.connect(self._query_wizpr_proxy)

        self.wizpr_version_btn = QtWidgets.QPushButton("Version")
        self.wizpr_version_btn.clicked.connect(self._query_wizpr_version)

        self.wizpr_lock_btn = QtWidgets.QPushButton("Lock")
        self.wizpr_lock_btn.clicked.connect(self._lock_wizpr)

        self.wizpr_sleep_btn = QtWidgets.QPushButton("Sleep")
        self.wizpr_sleep_btn.clicked.connect(self._sleep_wizpr)
        advanced.addWidget(
            self._button_grid_widget(
                [
                    self.wizpr_guided_btn,
                    self.wizpr_subscribe_btn,
                    self.wizpr_battery_btn,
                    self.wizpr_proxy_btn,
                    self.wizpr_version_btn,
                    self.wizpr_lock_btn,
                    self.wizpr_sleep_btn,
                ],
                columns=4,
            )
        )

        self.wizpr_proximity_status = QtWidgets.QLabel("Proximity: --")
        self.wizpr_proximity_status.setWordWrap(True)
        advanced.addWidget(self.wizpr_proximity_status)

        advanced_outer.addWidget(self.advanced_ble_body)
        self.advanced_ble_body.setVisible(False)
        self.advanced_ble_box.toggled.connect(self.advanced_ble_body.setVisible)
        self.advanced_ble_mount_lay.addWidget(self.advanced_ble_box)

        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        split.setChildrenCollapsible(False)
        self.ble_debug_split = split

        # devices table
        self.ble_table = QtWidgets.QTableWidget(0, 5)
        self.ble_table.setHorizontalHeaderLabels(["Type", "Name", "Address", "Signal", "Details"])
        self.ble_table.horizontalHeader().setStretchLastSection(True)
        self.ble_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.ble_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.ble_table.setMinimumHeight(150)
        split.addWidget(self.ble_table)

        # gatt inspector
        gbox = QtWidgets.QGroupBox("GATT Inspector")
        gbox.setMinimumHeight(330)
        g_lay = QtWidgets.QVBoxLayout(gbox)

        g_row = QtWidgets.QHBoxLayout()
        self.gatt_refresh_btn = QtWidgets.QPushButton("Refresh Services")
        self.gatt_refresh_btn.clicked.connect(self._refresh_gatt)
        g_row.addWidget(self.gatt_refresh_btn)

        self.gatt_sub_btn = QtWidgets.QPushButton("Subscribe Notify")
        self.gatt_sub_btn.clicked.connect(self._subscribe_selected_char)
        g_row.addWidget(self.gatt_sub_btn)

        self.gatt_unsub_btn = QtWidgets.QPushButton("Unsubscribe")
        self.gatt_unsub_btn.clicked.connect(self._unsubscribe_selected_char)
        g_row.addWidget(self.gatt_unsub_btn)

        g_row.addStretch(1)
        g_lay.addLayout(g_row)

        self.gatt_tree = QtWidgets.QTreeWidget()
        self.gatt_tree.setHeaderLabels(["UUID / Description", "Properties"])
        self.gatt_tree.header().setStretchLastSection(True)
        g_lay.addWidget(self.gatt_tree, 1)

        self.notify_box = QtWidgets.QPlainTextEdit()
        self.notify_box.setReadOnly(True)
        self.notify_box.setMaximumBlockCount(2000)
        self.notify_box.setMinimumHeight(120)
        g_lay.addWidget(QtWidgets.QLabel("Notifications"))
        g_lay.addWidget(self.notify_box, 1)

        split.addWidget(gbox)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        self.advanced_ble_mount_lay.addWidget(split, 1)
        self._advanced_ble_built = True
        visible = bool(self.cfg.show_advanced_options)
        self.advanced_ble_box.setVisible(visible)
        self.ble_debug_split.setVisible(visible)
        if self._ble_log_backlog:
            for line in self._ble_log_backlog:
                self.notify_box.appendPlainText(line)
            self._ble_log_backlog.clear()

    def _build_tab_llm(self) -> None:
        tab = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(tab)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(10)

        self.llm_tabs = QtWidgets.QTabWidget()
        self.llm_tabs.setUsesScrollButtons(True)

        self.llm_tabs.addTab(self._llm_single_group_page(self._build_openai_group()), "OpenAI")
        self.llm_tabs.addTab(self._llm_single_group_page(self._build_ollama_group()), "Ollama")
        self.llm_tabs.addTab(self._llm_single_group_page(self._build_compat_group()), "Compatible")
        self.llm_tabs.addTab(self._llm_single_group_page(self._build_codex_group()), "Codex CLI")
        self.llm_tabs.addTab(self._llm_single_group_page(self._build_opencode_group()), "OpenCode")
        self.llm_tabs.addTab(self._llm_single_group_page(self._build_transcription_group()), "Voice")
        lay.addWidget(self.llm_tabs, 1)

        self.llm_tab_index = self.tabs.addTab(tab, "Providers")

    def _llm_single_group_page(self, group: QtWidgets.QWidget) -> QtWidgets.QScrollArea:
        page = QtWidgets.QWidget()
        page_lay = QtWidgets.QVBoxLayout(page)
        page_lay.setContentsMargins(10, 10, 10, 10)
        page_lay.setSpacing(12)
        group.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Maximum)
        page_lay.addWidget(group)
        page_lay.addStretch(1)
        return self._scroll_tab(page)

    def _build_openai_group(self) -> QtWidgets.QGroupBox:
        gb = QtWidgets.QGroupBox("OpenAI")
        form = self._settings_form(gb)

        self.openai_key = QtWidgets.QLineEdit(self.cfg.openai.api_key)
        self.openai_key.setEchoMode(QtWidgets.QLineEdit.Password)
        form.addRow("API Key:", self.openai_key)

        self.openai_base = QtWidgets.QLineEdit(self.cfg.openai.base_url)
        self.openai_base.setPlaceholderText("(optional) https://api.openai.com/v1")
        form.addRow("Base URL:", self.openai_base)

        self.openai_model = QtWidgets.QComboBox()
        self.openai_model.setEditable(True)
        self.openai_model.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        self.openai_model.addItem(self.cfg.openai.model)
        self.openai_model.setCurrentText(self.cfg.openai.model)

        self.openai_fetch = QtWidgets.QPushButton("Fetch Models")
        self.openai_fetch.clicked.connect(self._refresh_openai_models)

        form.addRow("Model:", self._field_with_actions(self.openai_model, [self.openai_fetch]))

        self.openai_temp = QtWidgets.QDoubleSpinBox()
        self.openai_temp.setRange(0.0, 2.0)
        self.openai_temp.setSingleStep(0.05)
        self.openai_temp.setValue(0.7)
        form.addRow("Temperature:", self.openai_temp)

        self.openai_save = QtWidgets.QPushButton("Save OpenAI Settings")
        self.openai_save.clicked.connect(self._save_openai)

        self.openai_health = QtWidgets.QPushButton("Health Check")
        self.openai_health.clicked.connect(lambda: self._health_check("openai"))
        self.openai_use_talk = self._provider_talk_button("openai")
        form.addRow(
            "",
            self._button_grid_widget(
                [self.openai_save, self.openai_health, self.openai_use_talk],
                columns=3,
            ),
        )

        return gb

    def _build_opencode_group(self) -> QtWidgets.QGroupBox:
        gb = QtWidgets.QGroupBox("OpenCode Bridge")
        form = self._settings_form(gb)

        detected = self.cfg.opencode.executable or detect_opencode_executable()
        self.opencode_exe = QtWidgets.QLineEdit(detected)
        self.opencode_exe.setMinimumWidth(0)
        self.opencode_detect = QtWidgets.QPushButton("Detect")
        self.opencode_detect.clicked.connect(self._detect_opencode)
        form.addRow("Executable:", self._field_with_actions(self.opencode_exe, [self.opencode_detect]))

        self.opencode_model = QtWidgets.QComboBox()
        self.opencode_model.setEditable(True)
        self.opencode_model.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        self.opencode_model.addItem(self.cfg.opencode.model)
        self.opencode_model.setCurrentText(self.cfg.opencode.model)
        self.opencode_fetch = QtWidgets.QPushButton("Fetch Models")
        self.opencode_fetch.clicked.connect(self._refresh_opencode_models)
        form.addRow("Model:", self._field_with_actions(self.opencode_model, [self.opencode_fetch]))

        self.opencode_workspace = QtWidgets.QLineEdit(self.cfg.opencode.working_dir or str(Path.cwd()))
        self.opencode_workspace.setMinimumWidth(0)
        form.addRow("Workspace:", self.opencode_workspace)

        self.opencode_continue = QtWidgets.QCheckBox("Continue last session")
        self.opencode_continue.setChecked(bool(self.cfg.opencode.continue_session))
        form.addRow("", self.opencode_continue)

        self.opencode_auto = QtWidgets.QCheckBox("Auto-approve OpenCode actions")
        self.opencode_auto.setChecked(bool(self.cfg.opencode.auto_approve))
        form.addRow("", self.opencode_auto)

        self.opencode_timeout = QtWidgets.QDoubleSpinBox()
        self.opencode_timeout.setRange(30.0, 3600.0)
        self.opencode_timeout.setSingleStep(30.0)
        self.opencode_timeout.setValue(float(self.cfg.opencode.timeout_seconds or 600.0))
        form.addRow("Timeout (sec):", self.opencode_timeout)

        self.opencode_save = QtWidgets.QPushButton("Save OpenCode Settings")
        self.opencode_save.clicked.connect(self._save_opencode)

        self.opencode_test = QtWidgets.QPushButton("Test")
        self.opencode_test.clicked.connect(self._test_opencode)

        self.opencode_use_audio = QtWidgets.QPushButton("Use for Ring Voice")
        self.opencode_use_audio.clicked.connect(self._use_opencode_for_ring_voice)
        form.addRow(
            "",
            self._button_grid_widget(
                [self.opencode_save, self.opencode_test, self.opencode_use_audio],
                columns=3,
            ),
        )

        return gb

    def _build_codex_group(self) -> QtWidgets.QGroupBox:
        gb = QtWidgets.QGroupBox("Codex CLI")
        form = self._settings_form(gb)

        detected = self.cfg.codex.executable or detect_codex_executable()
        self.codex_exe = QtWidgets.QLineEdit(detected)
        self.codex_exe.setMinimumWidth(0)
        self.codex_detect = QtWidgets.QPushButton("Detect")
        self.codex_detect.clicked.connect(self._detect_codex)
        form.addRow("Executable:", self._field_with_actions(self.codex_exe, [self.codex_detect]))

        self.codex_model = QtWidgets.QLineEdit(self.cfg.codex.model)
        self.codex_model.setPlaceholderText("(default from Codex config)")
        form.addRow("Model:", self.codex_model)

        self.codex_workspace = QtWidgets.QLineEdit(self.cfg.codex.working_dir or str(Path.cwd()))
        self.codex_workspace.setMinimumWidth(0)
        form.addRow("Workspace:", self.codex_workspace)

        self.codex_sandbox = QtWidgets.QComboBox()
        self.codex_sandbox.addItems(["read-only", "workspace-write", "danger-full-access"])
        self.codex_sandbox.setCurrentText(self.cfg.codex.sandbox or "workspace-write")
        form.addRow("Sandbox:", self.codex_sandbox)

        self.codex_timeout = QtWidgets.QDoubleSpinBox()
        self.codex_timeout.setRange(30.0, 3600.0)
        self.codex_timeout.setSingleStep(30.0)
        self.codex_timeout.setValue(float(self.cfg.codex.timeout_seconds or 300.0))
        form.addRow("Timeout (sec):", self.codex_timeout)

        self.codex_save = QtWidgets.QPushButton("Save Codex Settings")
        self.codex_save.clicked.connect(self._save_codex)

        self.codex_use_audio = QtWidgets.QPushButton("Use for Ring Voice")
        self.codex_use_audio.clicked.connect(self._use_codex_for_ring_voice)
        form.addRow("", self._button_grid_widget([self.codex_save, self.codex_use_audio], columns=2))

        return gb

    def _build_ollama_group(self) -> QtWidgets.QGroupBox:
        gb = QtWidgets.QGroupBox("Ollama (local)")
        form = self._settings_form(gb)

        self.ollama_url = QtWidgets.QLineEdit(self.cfg.ollama.base_url)
        self.ollama_find = QtWidgets.QPushButton("Find Server")
        self.ollama_find.clicked.connect(self._find_ollama_server)

        form.addRow("Base URL:", self._field_with_actions(self.ollama_url, [self.ollama_find]))

        self.ollama_model = QtWidgets.QComboBox()
        self.ollama_model.setEditable(True)
        self.ollama_model.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        self.ollama_model.addItem(self.cfg.ollama.model)
        self.ollama_model.setCurrentText(self.cfg.ollama.model)

        self.ollama_fetch = QtWidgets.QPushButton("Fetch Models")
        self.ollama_fetch.clicked.connect(self._refresh_ollama_models)

        form.addRow("Model:", self._field_with_actions(self.ollama_model, [self.ollama_fetch]))

        self.ollama_temp = QtWidgets.QDoubleSpinBox()
        self.ollama_temp.setRange(0.0, 2.0)
        self.ollama_temp.setSingleStep(0.05)
        self.ollama_temp.setValue(0.7)
        form.addRow("Temperature:", self.ollama_temp)

        self.ollama_save = QtWidgets.QPushButton("Save Ollama Settings")
        self.ollama_save.clicked.connect(self._save_ollama)

        self.ollama_health = QtWidgets.QPushButton("Health Check")
        self.ollama_health.clicked.connect(lambda: self._health_check("ollama"))
        self.ollama_use_talk = self._provider_talk_button("ollama")
        form.addRow(
            "",
            self._button_grid_widget(
                [self.ollama_save, self.ollama_health, self.ollama_use_talk],
                columns=3,
            ),
        )

        return gb

    def _build_compat_group(self) -> QtWidgets.QGroupBox:
        gb = QtWidgets.QGroupBox("OpenAI-Compatible Server")
        form = self._settings_form(gb)

        self.compat_url = QtWidgets.QLineEdit(self.cfg.openai_compat.base_url)
        form.addRow("Base URL:", self.compat_url)

        self.compat_key = QtWidgets.QLineEdit(self.cfg.openai_compat.api_key)
        self.compat_key.setEchoMode(QtWidgets.QLineEdit.Password)
        form.addRow("API Key:", self.compat_key)

        self.compat_model = QtWidgets.QComboBox()
        self.compat_model.setEditable(True)
        self.compat_model.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        if self.cfg.openai_compat.model:
            self.compat_model.addItem(self.cfg.openai_compat.model)
            self.compat_model.setCurrentText(self.cfg.openai_compat.model)

        self.compat_fetch = QtWidgets.QPushButton("Fetch Models")
        self.compat_fetch.clicked.connect(self._refresh_compat_models)

        form.addRow("Model:", self._field_with_actions(self.compat_model, [self.compat_fetch]))

        self.compat_temp = QtWidgets.QDoubleSpinBox()
        self.compat_temp.setRange(0.0, 2.0)
        self.compat_temp.setSingleStep(0.05)
        self.compat_temp.setValue(0.7)
        form.addRow("Temperature:", self.compat_temp)

        self.compat_save = QtWidgets.QPushButton("Save Compat Settings")
        self.compat_save.clicked.connect(self._save_compat)

        self.compat_health = QtWidgets.QPushButton("Health Check")
        self.compat_health.clicked.connect(lambda: self._health_check("openai_compat"))
        self.compat_use_talk = self._provider_talk_button("openai_compat")
        form.addRow(
            "",
            self._button_grid_widget(
                [self.compat_save, self.compat_health, self.compat_use_talk],
                columns=3,
            ),
        )

        return gb

    def _build_transcription_group(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        def group_form(title: str) -> tuple[QtWidgets.QGroupBox, QtWidgets.QFormLayout]:
            gb = QtWidgets.QGroupBox(title)
            form = self._settings_form(gb)
            return gb, form

        stt_box, stt = group_form("Speech to Text")
        self.transcription_backend = QtWidgets.QComboBox()
        self.transcription_backend.addItem("Automatic (OpenAI when configured)", "auto")
        self.transcription_backend.addItem("Local faster-whisper", "local")
        self.transcription_backend.addItem("OpenAI", "openai")
        self._set_combo_data(self.transcription_backend, self.cfg.transcription.stt_backend, "auto")
        stt.addRow("Transcription Engine:", self.transcription_backend)

        self.openai_transcription_model = QtWidgets.QLineEdit(self.cfg.openai.transcription_model)
        stt.addRow("OpenAI STT Model:", self.openai_transcription_model)

        self.local_transcription_model = QtWidgets.QComboBox()
        self.local_transcription_model.setEditable(True)
        self.local_transcription_model.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        local_models = ["small.en", "base.en", "tiny.en"]
        self.local_transcription_model.addItems(local_models)
        if self.cfg.transcription.local_model not in local_models:
            self.local_transcription_model.insertItem(0, self.cfg.transcription.local_model)
        self.local_transcription_model.setCurrentText(self.cfg.transcription.local_model or "small.en")
        stt.addRow("Local Whisper Model:", self.local_transcription_model)

        self.local_transcription_compute = QtWidgets.QComboBox()
        self.local_transcription_compute.addItems(["int8", "float16", "float32"])
        self.local_transcription_compute.setCurrentText(self.cfg.transcription.local_compute_type or "int8")
        stt.addRow("Local Compute:", self.local_transcription_compute)

        self.transcription_warm_start = QtWidgets.QCheckBox("Warm local transcriber at startup")
        self.transcription_warm_start.setChecked(bool(self.cfg.transcription.warm_at_startup))
        stt.addRow("", self.transcription_warm_start)

        self.transcription_warm_after_connect = QtWidgets.QCheckBox("Warm local transcriber after ring connects")
        self.transcription_warm_after_connect.setChecked(bool(self.cfg.transcription.warm_after_connect))
        stt.addRow("", self.transcription_warm_after_connect)
        outer.addWidget(stt_box)

        route_box, route = group_form("Wake and Routing")
        self.transcription_require_wake = QtWidgets.QCheckBox("Require wake phrase before auto-sending")
        self.transcription_require_wake.setChecked(bool(self.cfg.transcription.require_wake_word))
        route.addRow("", self.transcription_require_wake)

        self.transcription_hold_coding = QtWidgets.QCheckBox("Hold Codex/OpenCode voice commands for review")
        self.transcription_hold_coding.setChecked(bool(self.cfg.transcription.hold_coding_voice_commands))
        route.addRow("", self.transcription_hold_coding)

        self.transcription_assistant_wake = QtWidgets.QLineEdit(self.cfg.transcription.assistant_wake_word or "Wizpr, Assistant")
        self.transcription_assistant_wake.setPlaceholderText("Wizpr, Assistant")
        route.addRow("Assistant Wake Phrase(s):", self.transcription_assistant_wake)

        self.transcription_codex_wake = QtWidgets.QLineEdit(self.cfg.transcription.codex_wake_word or "Codex")
        self.transcription_codex_wake.setPlaceholderText("Codex")
        route.addRow("Codex Wake Phrase(s):", self.transcription_codex_wake)

        self.transcription_opencode_wake = QtWidgets.QLineEdit(self.cfg.transcription.opencode_wake_word or "OpenCode, Open Code")
        self.transcription_opencode_wake.setPlaceholderText("OpenCode, Open Code")
        route.addRow("OpenCode Wake Phrase(s):", self.transcription_opencode_wake)

        self.transcription_clipboard_wake = QtWidgets.QLineEdit(self.cfg.transcription.clipboard_wake_word or "Wizpr")
        self.transcription_clipboard_wake.setPlaceholderText("Wizpr")
        route.addRow("Copy Text Wake Phrase(s):", self.transcription_clipboard_wake)

        self.transcription_paste_wake = QtWidgets.QLineEdit(self.cfg.transcription.paste_wake_word or "Wizpr")
        self.transcription_paste_wake.setPlaceholderText("Wizpr")
        route.addRow("Voice Keyboard Wake Phrase(s):", self.transcription_paste_wake)
        outer.addWidget(route_box)

        ring_box, ring = group_form("Ring Audio")
        self.transcription_finalize_delay = QtWidgets.QSpinBox()
        self.transcription_finalize_delay.setRange(0, 2000)
        self.transcription_finalize_delay.setSingleStep(50)
        self.transcription_finalize_delay.setSuffix(" ms")
        self.transcription_finalize_delay.setValue(max(0, int(self.cfg.transcription.ring_audio_finalize_delay_ms)))
        ring.addRow("Ring Stop Grace:", self.transcription_finalize_delay)

        self.transcription_voice_mode = self._ring_voice_mode_combo()
        ring.addRow("Voice Mode:", self.transcription_voice_mode)

        self.transcription_sleep_timeout = self._ring_sleep_timeout_combo()
        ring.addRow("Ring Sleep Timeout:", self.transcription_sleep_timeout)

        self.audio_preflight_enabled = QtWidgets.QCheckBox("Ignore quiet/non-speech captures before transcription")
        self.audio_preflight_enabled.setChecked(bool(self.cfg.transcription.audio_preflight_enabled))
        ring.addRow("", self.audio_preflight_enabled)

        self.audio_preflight_min_seconds = QtWidgets.QDoubleSpinBox()
        self.audio_preflight_min_seconds.setRange(0.05, 3.0)
        self.audio_preflight_min_seconds.setSingleStep(0.05)
        self.audio_preflight_min_seconds.setDecimals(2)
        self.audio_preflight_min_seconds.setSuffix(" sec")
        self.audio_preflight_min_seconds.setValue(float(self.cfg.transcription.audio_preflight_min_seconds or 0.35))
        ring.addRow("Min Capture:", self.audio_preflight_min_seconds)

        self.audio_preflight_min_active = QtWidgets.QDoubleSpinBox()
        self.audio_preflight_min_active.setRange(0.0, 3.0)
        self.audio_preflight_min_active.setSingleStep(0.05)
        self.audio_preflight_min_active.setDecimals(2)
        self.audio_preflight_min_active.setSuffix(" sec active")
        self.audio_preflight_min_active.setValue(float(self.cfg.transcription.audio_preflight_min_active_seconds))
        ring.addRow("Min Speech:", self.audio_preflight_min_active)

        self.ring_connection_sound = QtWidgets.QCheckBox("Play sound when ring connects")
        ring.addRow("", self.ring_connection_sound)

        self.mic_activation_sound = QtWidgets.QCheckBox("Play sound when ring mic starts/stops")
        ring.addRow("", self.mic_activation_sound)

        self.low_battery_warning = QtWidgets.QCheckBox("Show low battery warning")
        ring.addRow("", self.low_battery_warning)

        self.protect_ring_buttons_check = QtWidgets.QCheckBox("Protect connected ring from button lock/sleep actions")
        self.protect_ring_buttons_check.setChecked(bool(self.cfg.protect_connected_ring_buttons))
        self.protect_ring_buttons_check.setToolTip(
            "Keeps ring button events from sending software lock or sleep commands while connected."
        )
        ring.addRow("", self.protect_ring_buttons_check)
        self._sync_ring_settings_ui()
        outer.addWidget(ring_box)

        tts_box, tts = group_form("Spoken Responses")
        self.tts_voice = QtWidgets.QLineEdit(self.cfg.transcription.tts_voice)
        self.tts_voice.setPlaceholderText("(Auto: clearer Windows voice if available)")
        tts.addRow("Response Voice:", self.tts_voice)

        self.tts_rate = QtWidgets.QSpinBox()
        self.tts_rate.setRange(-10, 10)
        self.tts_rate.setValue(int(self.cfg.transcription.tts_rate or 0))
        tts.addRow("Response Rate:", self.tts_rate)
        outer.addWidget(tts_box)

        self.transcription_save = QtWidgets.QPushButton("Save Voice Settings")
        self.transcription_save.clicked.connect(lambda: self._save_transcription(show_status=True))

        self.transcription_warm = QtWidgets.QPushButton("Warm Now")
        self.transcription_warm.clicked.connect(self._warm_transcriber_now)
        outer.addWidget(self._button_grid_widget([self.transcription_save, self.transcription_warm], columns=2))
        return page

    def _build_tab_chat(self) -> None:
        tab = QtWidgets.QWidget()
        tab.setObjectName('chatPage')
        lay = QtWidgets.QVBoxLayout(tab)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        self.chat_split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.chat_split.setObjectName('chatSplit')
        self.chat_split.setChildrenCollapsible(False)
        self.chat_split.setHandleWidth(7)

        conversation_panel = QtWidgets.QFrame()
        conversation_panel.setObjectName('conversationCard')
        conversation_lay = QtWidgets.QVBoxLayout(conversation_panel)
        conversation_lay.setContentsMargins(14, 12, 14, 12)
        conversation_lay.setSpacing(8)
        conversation_header = QtWidgets.QHBoxLayout()
        heading_box = QtWidgets.QVBoxLayout()
        heading_box.setSpacing(0)
        heading = QtWidgets.QLabel('Conversation')
        heading.setObjectName('sectionTitle')
        heading_box.addWidget(heading)
        hint = QtWidgets.QLabel('Live responses from your selected assistant')
        hint.setObjectName('sectionSubtitle')
        heading_box.addWidget(hint)
        conversation_header.addLayout(heading_box)
        conversation_header.addStretch(1)
        self.clear_output_btn = QtWidgets.QPushButton('Clear')
        self.clear_output_btn.setObjectName('quietButton')
        self.clear_output_btn.clicked.connect(lambda: self.output.setPlainText(''))
        conversation_header.addWidget(self.clear_output_btn)
        conversation_lay.addLayout(conversation_header)

        self.output = ConversationView()
        self.output.setObjectName('conversationView')
        self.output.setMaximumBlockCount(3000)
        self.output.setMinimumHeight(270)
        conversation_lay.addWidget(self.output, 1)
        self.chat_split.addWidget(conversation_panel)

        composer_panel = QtWidgets.QFrame()
        composer_panel.setObjectName('composerCard')
        composer_lay = QtWidgets.QVBoxLayout(composer_panel)
        composer_lay.setContentsMargins(14, 10, 14, 12)
        composer_lay.setSpacing(8)

        voice_bar = QtWidgets.QHBoxLayout()
        self.voice_waveform = VoiceWaveformWidget()
        voice_bar.addWidget(self.voice_waveform, 1)
        self.chat_voice_status_label = QtWidgets.QLabel('Ready')
        self.chat_voice_status_label.setObjectName('listeningLabel')
        self.chat_voice_status_label.setMinimumWidth(160)
        self.chat_voice_status_label.setAlignment(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft)
        voice_bar.addWidget(self.chat_voice_status_label)
        composer_lay.addLayout(voice_bar)

        prompt_row = QtWidgets.QHBoxLayout()
        prompt_row.setSpacing(10)
        self.prompt = QtWidgets.QPlainTextEdit()
        self.prompt.setObjectName('promptInput')
        self.prompt.setPlaceholderText('Ask Wizpr anything…')
        self.prompt.setLineWrapMode(QtWidgets.QPlainTextEdit.WidgetWidth)
        self.prompt.setTabChangesFocus(True)
        self.prompt.installEventFilter(self)
        self.prompt.textChanged.connect(self._update_talk_action_state)
        self.prompt.setMinimumHeight(70)
        self.prompt.setMaximumHeight(118)
        prompt_row.addWidget(self.prompt, 1)

        self.listen_btn = QtWidgets.QPushButton('◉')
        self.listen_btn.setObjectName('micButton')
        self.listen_btn.setToolTip('Connect the ring, or confirm it is ready for voice input.')
        self.listen_btn.clicked.connect(self._handle_mic_button)
        self.listen_btn.setFixedSize(58, 58)
        prompt_row.addWidget(self.listen_btn, 0, QtCore.Qt.AlignVCenter)
        composer_lay.addLayout(prompt_row)

        action_row = QtWidgets.QHBoxLayout()
        action_row.setSpacing(7)
        self.send_btn = QtWidgets.QPushButton('Send')
        self.send_btn.setObjectName('primaryButton')
        self.send_btn.setDefault(True)
        self.send_btn.clicked.connect(self._send_chat)
        action_row.addWidget(self.send_btn)
        self.clear_btn = QtWidgets.QPushButton('Clear Prompt')
        self.clear_btn.setObjectName('quietButton')
        self.clear_btn.clicked.connect(lambda: self.prompt.setPlainText(''))
        action_row.addWidget(self.clear_btn)

        self.send_last_btn = QtWidgets.QPushButton('Last Transcript')
        self.send_last_btn.setToolTip('Send the last voice transcript to the active assistant.')
        self.send_last_btn.clicked.connect(self._send_last_transcript)
        action_row.addWidget(self.send_last_btn)
        self.send_codex_btn = QtWidgets.QPushButton('Codex')
        self.send_codex_btn.clicked.connect(self._send_current_to_codex)
        action_row.addWidget(self.send_codex_btn)
        self.send_opencode_btn = QtWidgets.QPushButton('OpenCode')
        self.send_opencode_btn.clicked.connect(self._send_current_to_opencode)
        action_row.addWidget(self.send_opencode_btn)
        self.copy_text_btn = QtWidgets.QPushButton('Copy')
        self.copy_text_btn.clicked.connect(self._copy_current_text)
        action_row.addWidget(self.copy_text_btn)
        action_row.addStretch(1)
        settings_shortcut = QtWidgets.QPushButton('Voice Settings')
        settings_shortcut.setObjectName('quietButton')
        settings_shortcut.clicked.connect(lambda: self._open_settings('voice'))
        action_row.addWidget(settings_shortcut)
        composer_lay.addLayout(action_row)

        self.chat_split.addWidget(composer_panel)
        self.chat_split.setStretchFactor(0, 1)
        self.chat_split.setStretchFactor(1, 0)
        self.chat_split.setSizes([500, 185])
        lay.addWidget(self.chat_split, 1)
        self._update_talk_action_state()

        self.chat_tab_index = self.tabs.addTab(tab, 'Talk')

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if obj is getattr(self, "prompt", None) and event.type() == QtCore.QEvent.KeyPress:
            key_event = event
            if key_event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
                if key_event.modifiers() & QtCore.Qt.ShiftModifier:
                    return False
                self._send_chat()
                return True
        return super().eventFilter(obj, event)

    def _ensure_advanced_tabs_built(self) -> None:
        if getattr(self, "_advanced_tabs_built", False):
            return
        self._build_tab_llm()
        self._build_tab_bridge()
        self._build_tab_mappings()
        self._build_tab_logs()
        self._advanced_tabs_built = True

    def _build_tab_bridge(self) -> None:
        tab = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(tab)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(10)

        settings = QtWidgets.QGroupBox("Mobile / App Bridge")
        form = QtWidgets.QFormLayout(settings)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.ExpandingFieldsGrow)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.WrapLongRows)

        self.bridge_enabled = QtWidgets.QCheckBox("Start bridge when Wizpr Suite opens")
        self.bridge_enabled.setChecked(bool(self.cfg.mobile_bridge.enabled))
        form.addRow("", self.bridge_enabled)

        self.bridge_host = QtWidgets.QLineEdit(self.cfg.mobile_bridge.host or "127.0.0.1")
        self.bridge_host.setPlaceholderText("127.0.0.1")
        form.addRow("Host:", self.bridge_host)

        self.bridge_port = QtWidgets.QSpinBox()
        self.bridge_port.setRange(1, 65535)
        self.bridge_port.setValue(int(self.cfg.mobile_bridge.port or 8844))
        form.addRow("Port:", self.bridge_port)

        self.bridge_token = QtWidgets.QLineEdit(self.cfg.mobile_bridge.token)
        self.bridge_token.setEchoMode(QtWidgets.QLineEdit.Password)
        self.bridge_token.setPlaceholderText("required when host is not localhost")
        form.addRow("Token:", self.bridge_token)

        self.bridge_require_approval = QtWidgets.QCheckBox("Require approval before running phone commands")
        self.bridge_require_approval.setChecked(bool(self.cfg.mobile_bridge.require_approval))
        form.addRow("", self.bridge_require_approval)

        self.bridge_status = QtWidgets.QLabel("Stopped")
        form.addRow("Status:", self.bridge_status)

        self.bridge_phone_url = QtWidgets.QLineEdit()
        self.bridge_phone_url.setReadOnly(True)
        self.bridge_phone_url.setMinimumWidth(0)
        form.addRow("Phone URL:", self.bridge_phone_url)

        self.bridge_save = QtWidgets.QPushButton("Save")
        self.bridge_save.clicked.connect(self._save_mobile_bridge)

        self.bridge_token_btn = QtWidgets.QPushButton("Generate Token")
        self.bridge_token_btn.clicked.connect(self._generate_mobile_bridge_token)

        self.bridge_start = QtWidgets.QPushButton("Start")
        self.bridge_start.clicked.connect(self._start_mobile_bridge_clicked)

        self.bridge_stop = QtWidgets.QPushButton("Stop")
        self.bridge_stop.clicked.connect(self._stop_mobile_bridge_clicked)

        self.bridge_copy_url = QtWidgets.QPushButton("Copy URL")
        self.bridge_copy_url.clicked.connect(self._copy_mobile_bridge_url)

        self.bridge_open_page = QtWidgets.QPushButton("Open Page")
        self.bridge_open_page.clicked.connect(self._open_mobile_bridge_page)
        form.addRow(
            "",
            self._button_grid_widget(
                [
                    self.bridge_save,
                    self.bridge_token_btn,
                    self.bridge_start,
                    self.bridge_stop,
                    self.bridge_copy_url,
                    self.bridge_open_page,
                ],
                columns=3,
            ),
        )

        lay.addWidget(settings)

        self.bridge_pending = QtWidgets.QTableWidget(0, 3)
        self.bridge_pending.setHorizontalHeaderLabels(["Target", "Source", "Text"])
        self.bridge_pending.horizontalHeader().setStretchLastSection(True)
        self.bridge_pending.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.bridge_pending.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.bridge_pending.setWordWrap(False)
        self.bridge_pending.setMinimumHeight(180)
        lay.addWidget(self.bridge_pending, 1)
        self._reload_bridge_pending_table()

        pending_btns = QtWidgets.QHBoxLayout()
        self.bridge_approve = QtWidgets.QPushButton("Run Selected")
        self.bridge_approve.clicked.connect(self._approve_mobile_bridge_request)
        pending_btns.addWidget(self.bridge_approve)

        self.bridge_reject = QtWidgets.QPushButton("Reject Selected")
        self.bridge_reject.clicked.connect(self._reject_mobile_bridge_request)
        pending_btns.addWidget(self.bridge_reject)
        pending_btns.addStretch(1)
        lay.addLayout(pending_btns)

        self._set_mobile_bridge_status()
        self.bridge_tab_index = self.tabs.addTab(self._scroll_tab(tab), "Bridge")

    def _build_tab_mappings(self) -> None:
        tab = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(tab)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(10)

        help_lbl = QtWidgets.QLabel("Map ring events/topics to actions. Topics: button_single, button_double, button_triple, button_quad, button_five, button_long, button_multi, sos, audio_capture, sleep, lock, power_off, raw_notify.")
        help_lbl.setWordWrap(True)
        lay.addWidget(help_lbl)

        self.map_table = QtWidgets.QTableWidget(0, 2)
        self.map_table.setHorizontalHeaderLabels(["Trigger Topic", "Action"])
        self.map_table.horizontalHeader().setStretchLastSection(True)
        self.map_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.map_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.map_table, 1)

        row = QtWidgets.QGridLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setHorizontalSpacing(8)
        row.setVerticalSpacing(8)
        self.map_trigger = QtWidgets.QLineEdit()
        self.map_trigger.setPlaceholderText("e.g. button_single")
        row.addWidget(self.map_trigger, 0, 0, 1, 2)

        self.map_action = QtWidgets.QComboBox()
        self.map_action.addItems(
            [
                "toggle_ring_lock",
                "start_new_chat",
                "edit_last_transcript",
                "toggle_listen",
                "send_last_transcript",
                "send_audio_to_assistant",
                "send_last_to_codex",
                "send_audio_to_codex",
                "send_last_to_opencode",
                "send_audio_to_opencode",
                "transcribe_audio_only",
                "copy_audio_to_clipboard",
                "paste_audio_to_active_app",
                "copy_last_transcript",
                "paste_last_transcript",
                "cycle_llm",
                "noop",
            ]
        )
        row.addWidget(self.map_action, 0, 2, 1, 2)

        self.map_add = QtWidgets.QPushButton("Add Mapping")
        self.map_add.clicked.connect(self._add_mapping)
        row.addWidget(self.map_add, 1, 0)

        self.map_remove = QtWidgets.QPushButton("Remove Selected")
        self.map_remove.clicked.connect(self._remove_mapping)
        row.addWidget(self.map_remove, 1, 1)

        row.setColumnStretch(0, 1)
        row.setColumnStretch(1, 1)
        row.setColumnStretch(2, 1)
        row.setColumnStretch(3, 1)
        lay.addLayout(row)

        self._reload_mapping_table()

        self.mappings_tab_index = self.tabs.addTab(self._scroll_tab(tab), "Mappings")

    def _build_tab_logs(self) -> None:
        tab = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(tab)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(10)
        lay.addWidget(self.log_box, 1)
        self.logs_tab_index = self.tabs.addTab(tab, "Logs")


    def _apply_theme(self) -> None:
        qss_path = Path(__file__).resolve().parents[1] / "resources" / ("theme_dark.qss" if self.cfg.theme == "dark" else "theme_light.qss")
        try:
            self.setStyleSheet(qss_path.read_text(encoding="utf-8"))
        except Exception:
            self.setStyleSheet("")
        self.statusBar().showMessage(f"Theme: {self.cfg.theme}", 1500)

    def _toggle_theme(self) -> None:
        self.cfg.theme = "light" if self.cfg.theme == "dark" else "dark"
        self._apply_theme()
        save_config(self.app_dir, self.cfg)

    def _advanced_changed(self, checked: bool) -> None:
        self.cfg.show_advanced_options = bool(checked)
        save_config(self.app_dir, self.cfg)
        if checked:
            self._ensure_advanced_tabs_built()
        self._set_advanced_visible(bool(checked))

    def _set_advanced_visible(self, visible: bool) -> None:
        if visible:
            self._ensure_advanced_tabs_built()
            self._ensure_advanced_ble_built()
        for attr in ("llm_tab_index", "bridge_tab_index", "mappings_tab_index", "logs_tab_index"):
            idx = getattr(self, attr, -1)
            if idx >= 0:
                self.tabs.setTabVisible(idx, visible)

        for attr in ("advanced_ble_box", "ble_debug_split"):
            widget = getattr(self, attr, None)
            if widget is not None:
                widget.setVisible(visible)

        if not visible:
            current = self.tabs.currentIndex()
            hidden = {
                getattr(self, "llm_tab_index", -1),
                getattr(self, "bridge_tab_index", -1),
                getattr(self, "mappings_tab_index", -1),
                getattr(self, "logs_tab_index", -1),
            }
            if current in hidden:
                self.tabs.setCurrentIndex(getattr(self, "chat_tab_index", 0))


    def _register_actions(self) -> None:
        async def _toggle_ring_lock(payload: dict[str, Any]) -> None:
            await self._toggle_ring_lock_from_button()

        async def _start_new_chat(payload: dict[str, Any]) -> None:
            self._start_new_chat_from_button()

        async def _edit_last_transcript(payload: dict[str, Any]) -> None:
            self._edit_last_transcript_from_button()

        async def _toggle_listen(payload: dict[str, Any]) -> None:
            self._toggle_listen()

        async def _send_last(payload: dict[str, Any]) -> None:
            self._send_last_transcript()

        async def _send_audio_assistant(payload: dict[str, Any]) -> None:
            await self._send_audio_capture_to_assistant(payload)

        async def _send_last_codex(payload: dict[str, Any]) -> None:
            await self._send_last_to_codex()

        async def _send_audio_codex(payload: dict[str, Any]) -> None:
            await self._send_audio_capture_to_codex(payload)

        async def _send_last_opencode(payload: dict[str, Any]) -> None:
            await self._send_last_to_opencode()

        async def _send_audio_opencode(payload: dict[str, Any]) -> None:
            await self._send_audio_capture_to_opencode(payload)

        async def _transcribe_audio_only(payload: dict[str, Any]) -> None:
            await self._transcribe_audio_capture_only(payload)

        async def _copy_audio_clipboard(payload: dict[str, Any]) -> None:
            await self._copy_audio_capture_to_clipboard(payload)

        async def _paste_audio_active_app(payload: dict[str, Any]) -> None:
            await self._paste_audio_capture_to_active_app(payload)

        async def _copy_last(payload: dict[str, Any]) -> None:
            self._copy_last_transcript()

        async def _paste_last(payload: dict[str, Any]) -> None:
            await self._paste_last_transcript()

        async def _cycle_llm(payload: dict[str, Any]) -> None:
            ids = self.registry.list_ids()
            if not ids:
                return
            cur = self.active_llm_id
            nxt = ids[(ids.index(cur) + 1) % len(ids)] if cur in ids else ids[0]
            self._set_active_llm_combo(nxt)
            self._on_active_llm_changed(nxt)

        async def _noop(payload: dict[str, Any]) -> None:
            return

        self.router.register_action_handler("toggle_ring_lock", _toggle_ring_lock)
        self.router.register_action_handler("start_new_chat", _start_new_chat)
        self.router.register_action_handler("edit_last_transcript", _edit_last_transcript)
        self.router.register_action_handler("toggle_listen", _toggle_listen)
        self.router.register_action_handler("send_last_transcript", _send_last)
        self.router.register_action_handler("send_audio_to_assistant", _send_audio_assistant)
        self.router.register_action_handler("send_last_to_codex", _send_last_codex)
        self.router.register_action_handler("send_audio_to_codex", _send_audio_codex)
        self.router.register_action_handler("send_last_to_opencode", _send_last_opencode)
        self.router.register_action_handler("send_audio_to_opencode", _send_audio_opencode)
        self.router.register_action_handler("transcribe_audio_only", _transcribe_audio_only)
        self.router.register_action_handler("copy_audio_to_clipboard", _copy_audio_clipboard)
        self.router.register_action_handler("paste_audio_to_active_app", _paste_audio_active_app)
        self.router.register_action_handler("copy_last_transcript", _copy_last)
        self.router.register_action_handler("paste_last_transcript", _paste_last)
        self.router.register_action_handler("cycle_llm", _cycle_llm)
        self.router.register_action_handler("noop", _noop)

    async def _wire_bus(self) -> None:
        async def _handle(topic: str, payload: Any) -> None:
            for action, triggers in (self.cfg.mappings or {}).items():
                if topic not in triggers:
                    continue
                if (
                    self.cfg.protect_connected_ring_buttons
                    and self._ring_is_connected()
                    and topic in BUTTON_TOPICS
                    and action in {"toggle_ring_lock"}
                ):
                    self._append_ring_activity("Connection protection ignored a ring button lock action.")
                    continue
                routed = {"action": action, "topic": topic, "payload": payload}
                if topic == "audio_capture" and action in VOICE_CAPTURE_ACTIONS:
                    self._schedule_voice_capture_action(action, routed)
                    continue
                await self.router.dispatch(action, routed)

        async def _mk(topic: str):
            async def _h(payload: Any) -> None:
                await _handle(topic, payload)
            return _h

        for topic in [
            "button_single",
            "button_double",
            "button_triple",
            "button_quad",
            "button_five",
            "button_long",
            "button_multi",
            "sos",
            "audio_capture",
            "sleep",
            "lock",
            "power_off",
            "raw_notify",
        ]:
            await self.bus.subscribe(topic, await _mk(topic))

        await self.bus.subscribe("raw_notify", self._on_raw_notify)
        for topic in [
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
        ]:
            await self.bus.subscribe(topic, await self._mk_notify_topic_handler(topic))
        await self.bus.subscribe("bridge_request", self._on_mobile_bridge_request)

    async def _mk_notify_topic_handler(self, topic: str):
        async def _h(payload: Any) -> None:
            if topic == "audio_capture" and isinstance(payload, dict) and payload.get("path"):
                self._last_audio_path = Path(str(payload["path"]))
            session_id = self._voice_session_from_payload(payload)
            if topic == "mic_pre_on":
                self._arm_voice_capture()
            elif topic == "mic_on":
                self._begin_voice_capture(session_id=session_id)
                self._start_ring_sleep_timeout_guard()
            elif topic in {"mic_off", "audio_capture"}:
                self._cancel_ring_sleep_timeout_guard()
            if self.cfg.transcription.mic_activation_sound:
                if topic == "mic_on":
                    self._play_feedback_sound("mic_on")
                elif topic == "mic_off":
                    self._play_feedback_sound("mic_off")
            self._update_basic_ring_status(topic, payload)
            if self.mobile_bridge.running:
                await self.mobile_bridge.publish_event(topic, payload)
            self._append_ring_activity(self._ring_activity_line(topic, payload))
            self._append_ble_log(f"{topic}: {payload}")
        return _h

    def _append_ring_activity(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        box = getattr(self, "ring_activity_box", None)
        if box is not None:
            box.appendPlainText(text)

    def _ring_activity_line(self, topic: str, payload: Any) -> str:
        if topic == "battery" and isinstance(payload, dict):
            level = payload.get("level")
            voltage = payload.get("voltage")
            if level is not None and voltage is not None:
                return f"Battery {level}% ({float(voltage):.2f}V)"
            return f"Battery {payload.get('text') or '--'}"
        if topic == "audio_capture" and isinstance(payload, dict):
            path = Path(str(payload.get("path") or ""))
            return f"Audio captured: {path.name or 'saved'}"
        labels = {
            "ring_event": "Ring event",
            "ring_command": "Command sent",
            "button_single": "Single click",
            "button_double": "Double click",
            "button_triple": "Triple click",
            "button_quad": "Four clicks",
            "button_five": "Five clicks",
            "button_long": "Long press",
            "button_multi": "Multi-click",
            "sos": "SOS click pattern",
            "mic_pre_on": "Mic ready",
            "mic_on": "Recording",
            "mic_off": "Recording stopped",
            "sleep": "Ring sleeping",
            "lock": "Ring lock toggled",
            "power_off": "Ring powered off",
            "version": "Version response",
            "proxy": "Proximity check",
        }
        label = labels.get(topic, topic)
        if isinstance(payload, dict):
            text = str(payload.get("text") or payload.get("command") or "").strip()
            if text:
                return f"{label}: {text}"
        return label

    def _update_basic_ring_status(self, topic: str, payload: Any) -> None:
        if topic == "battery" and isinstance(payload, dict):
            level = payload.get("level")
            voltage = payload.get("voltage")
            if level is not None and voltage is not None:
                level_int = int(level)
                battery_text = self._battery_status_text(level_int, float(voltage))
                self.ring_battery_status.setText(battery_text)
                sidebar_battery = getattr(self, "sidebar_battery_status", None)
                if sidebar_battery is not None:
                    sidebar_battery.setText(battery_text.replace("Battery: ", ""))
                self._handle_low_battery(level_int)
            else:
                text = str(payload.get("text", "")).strip()
                battery_text = f"Battery: {text or '--'}"
                self.ring_battery_status.setText(battery_text)
                sidebar_battery = getattr(self, "sidebar_battery_status", None)
                if sidebar_battery is not None:
                    sidebar_battery.setText(battery_text.replace("Battery: ", ""))
            self.ring_last_event.setText("Last event: battery")
            return

        labels = {
            "mic_pre_on": "mic ready",
            "mic_on": "recording",
            "mic_off": "recording stopped",
            "audio_capture": "audio captured",
            "version": "version",
            "proxy": "proximity check",
            "ring_event": "ring event",
            "button_single": "single click",
            "button_double": "double click",
            "button_triple": "triple click",
            "button_quad": "four clicks",
            "button_five": "five clicks",
            "button_long": "long press",
            "button_multi": "multi-click",
            "sos": "SOS click pattern",
            "sleep": "sleeping",
            "lock": "lock toggled",
            "power_off": "powered off",
        }
        label = labels.get(topic)
        if label:
            self.ring_last_event.setText(f"Last event: {label}")
        if topic == "mic_on":
            self._set_voice_status("Voice: ring is recording")
        elif topic == "mic_off":
            self._set_voice_status("Voice: recording stopped; saving audio")
        elif topic == "audio_capture":
            self._set_voice_status("Voice: audio captured; transcribing")
        if topic == "proxy" and isinstance(payload, dict):
            self._update_proximity_status(payload)

    def _update_proximity_status(self, payload: dict[str, Any]) -> None:
        text = str(payload.get("text") or "").strip()
        display = f"Proximity: {text or '--'}"
        label = getattr(self, "wizpr_proximity_status", None)
        if label is not None:
            label.setText(display)

    def _battery_status_text(self, level: int, voltage: float) -> str:
        text = f"Battery: {level}% ({voltage:.2f}V)"
        if self.cfg.transcription.low_battery_warning and level <= RING_LOW_BATTERY_PERCENT:
            return f"{text} - low"
        return text

    def _handle_low_battery(self, level: int) -> None:
        if not self.cfg.transcription.low_battery_warning:
            if self._ring_low_battery_active:
                self._ring_low_battery_active = False
                self._ring_low_battery_level = None
                self._refresh_quick_ready()
            return

        was_active = self._ring_low_battery_active
        if level <= RING_LOW_BATTERY_PERCENT:
            self._ring_low_battery_active = True
            self._ring_low_battery_level = level
            if not was_active:
                self._append_ring_activity(f"Low battery warning: {level}%")
                self.statusBar().showMessage(f"WIZPR Ring battery is low: {level}%.", 8000)
            self._refresh_quick_ready()
            return

        if level >= RING_LOW_BATTERY_CLEAR_PERCENT and was_active:
            self._ring_low_battery_active = False
            self._ring_low_battery_level = None
            self._append_ring_activity(f"Battery back above warning level: {level}%")
            self._refresh_quick_ready()

    async def _on_raw_notify(self, payload: Any) -> None:
        try:
            self._append_ble_log(str(payload))
        except Exception:
            pass

    def _ring_voice_target_from_mappings(self) -> str:
        mappings = self.cfg.mappings or {}
        if "audio_capture" in mappings.get("paste_audio_to_active_app", []):
            return "paste"
        if "audio_capture" in mappings.get("copy_audio_to_clipboard", []):
            return "clipboard"
        if "audio_capture" in mappings.get("send_audio_to_opencode", []):
            return "opencode"
        if "audio_capture" in mappings.get("transcribe_audio_only", []):
            return "transcript"
        if "audio_capture" in mappings.get("send_audio_to_codex", []):
            return "codex"
        return "assistant"

    def _ring_voice_target_value(self) -> str:
        target = (self.cfg.ring_voice_target or "").strip().lower()
        if target in {"assistant", "codex", "opencode", "transcript", "clipboard", "paste"}:
            return target
        return self._ring_voice_target_from_mappings()

    def _sync_ring_voice_target_ui(self) -> None:
        combo = getattr(self, "ring_voice_target", None)
        if combo is None:
            return
        target = self._ring_voice_target_value()
        idx = combo.findData(target)
        if idx < 0:
            idx = combo.findData("assistant")
        combo.blockSignals(True)
        try:
            combo.setCurrentIndex(idx)
        finally:
            combo.blockSignals(False)

    def _ring_voice_target_changed(self) -> None:
        target = self.ring_voice_target.currentData()
        self._set_ring_voice_target(str(target or "assistant"))

    def _set_voice_status(self, text: str) -> None:
        for attr in ("voice_status_label", "chat_voice_status_label"):
            label = getattr(self, attr, None)
            if label is not None:
                label.setText(text)
        lowered = (text or "").casefold()
        active = (
            any(
                token in lowered
                for token in (
                    "ring is recording",
                    "voice: listening",
                    "listening for interrupt phrase",
                    "transcribing",
                    "audio captured",
                    "understanding",
                )
            )
            and not any(
                token in lowered
                for token in ("stopped", "ignored", "error", "heard", "ready", "protected", "continues")
            )
        )
        self._voice_ui_active = active
        waveform = getattr(self, "voice_waveform", None)
        if waveform is not None:
            waveform.set_active(active)
        self._refresh_mic_button_state()

    def _voice_target_label(self) -> str:
        return {
            "assistant": "Assistant",
            "codex": "Codex",
            "opencode": "OpenCode",
            "transcript": "Transcript Only",
            "clipboard": "Copy Text",
            "paste": "Voice Keyboard",
        }.get(self._ring_voice_target_value(), "Assistant")

    def _ring_ready_text(self) -> str:
        status = getattr(self, "ring_connection_status", None)
        text = status.text().strip() if status is not None else ""
        if self._ring_low_battery_active and self._ring_low_battery_level is not None:
            return f"Ring low battery {self._ring_low_battery_level}%"
        if text and text not in {"Disconnected", "Remembered"}:
            return f"Ring {text.lower()}"
        if (self.cfg.last_ble_address or "").strip():
            return "Ring saved"
        return "Ring not saved"

    def _openai_transcription_key(self) -> str:
        return (
            (self.openai_key.text().strip() if hasattr(self, "openai_key") else "")
            or self.cfg.openai.api_key
            or os.environ.get("OPENAI_API_KEY", "").strip()
        )

    def _transcription_backend(self) -> str:
        backend = (self.cfg.transcription.stt_backend or "auto").strip().lower()
        if backend == "auto":
            return "openai" if self._openai_transcription_key() else "local"
        return backend if backend in {"local", "openai"} else "local"

    def _transcription_backend_label(self) -> str:
        configured = (self.cfg.transcription.stt_backend or "auto").strip().lower()
        if configured == "auto":
            return "automatic OpenAI STT" if self._openai_transcription_key() else f"automatic local STT {self.cfg.transcription.local_model or 'small.en'}"
        if self._transcription_backend() == "openai":
            if self._openai_transcription_key():
                return "OpenAI STT"
            return "OpenAI STT missing key"
        return f"local STT {self.cfg.transcription.local_model or 'small.en'}"

    def _voice_ready_text(self) -> str:
        stt = self._transcription_backend_label()
        wake = "wake on" if self._target_requires_wake_phrase(self._ring_voice_target_value()) else "wake off"
        speak = "speech on" if self.cfg.transcription.speak_responses else "speech off"
        interrupt = {
            "word": "interrupt phrase",
            "ring": "ring interrupt",
            "both": "phrase/ring interrupt",
            "off": "interrupt off",
        }.get(self.cfg.transcription.interrupt_mode, "interrupt phrase")
        return f"{self._voice_target_label()} | {wake} | {speak} | {interrupt} | {stt}"

    def _bridge_ready_text(self) -> str:
        pending = self._bridge_pending_count()
        if pending == 1:
            return "Bridge 1 approval pending"
        if pending > 1:
            return f"Bridge {pending} approvals pending"
        if self.mobile_bridge.running:
            return "Bridge running"
        if self.cfg.mobile_bridge.enabled:
            return "Bridge autostart"
        return "Bridge off"

    def _bridge_pending_count(self) -> int:
        return len(getattr(self, "_bridge_pending_by_id", {}) or {})

    def _mobile_bridge_status_payload(self) -> dict[str, Any]:
        ring_status = getattr(self, "ring_connection_status", None)
        battery = getattr(self, "ring_battery_status", None)
        last_event = getattr(self, "ring_last_event", None)
        target = self._ring_voice_target_value()
        phrase = ""
        if self._target_requires_wake_phrase(target):
            phrase = (self._wake_phrases_for_target(target) or [self._wake_phrase_for_target(target) or "Wizpr"])[0]
        return {
            "ready": self._quick_ready_text(),
            "next_step": self._next_step_text(),
            "ring": {
                "status": ring_status.text().strip() if ring_status is not None else "",
                "saved": bool((self.cfg.last_ble_address or "").strip()),
                "battery": battery.text().strip() if battery is not None else "",
                "last_event": last_event.text().strip() if last_event is not None else "",
                "keep_connected": bool(self._ring_keep_connected),
            },
            "voice": {
                "target": target,
                "target_label": self._voice_target_label(),
                "wake_required": self._target_requires_wake_phrase(target),
                "wake_phrase": phrase,
                "speak_responses": bool(self.cfg.transcription.speak_responses),
                "interrupt_mode": self.cfg.transcription.interrupt_mode,
                "interrupt_word": self.cfg.transcription.interrupt_word,
                "stt": self._transcription_backend(),
                "local_model": self.cfg.transcription.local_model or "small.en",
            },
            "assistant": {
                "active_llm": self.active_llm_id,
                "label": self._provider_label(self.active_llm_id),
                "detail": self._active_llm_detail_text(),
                "memory_enabled": bool(self.cfg.memory.enabled),
                "tool_permission": self.cfg.tools.permission_mode,
            },
        }

    def _quick_ready_text(self) -> str:
        return f"{self._ring_ready_text()} | {self._voice_ready_text()} | {self._bridge_ready_text()}"

    def _voice_cue_text(self) -> str:
        target = self._ring_voice_target_value()
        if target == "transcript":
            return "Voice cue: transcript only."
        if self._target_requires_wake_phrase(target):
            phrase = (self._wake_phrases_for_target(target) or [self._wake_phrase_for_target(target) or "Wizpr"])[0]
            return f"Voice cue: start with '{phrase}'."
        return "Voice cue: speak normally."

    def _next_step_text(self) -> str:
        status = getattr(self, "ring_connection_status", None)
        text = status.text().strip().casefold() if status is not None else ""
        saved = bool((self.cfg.last_ble_address or "").strip())
        pending_bridge = self._bridge_pending_count()

        if pending_bridge == 1:
            return "Next: review the pending Bridge command in Advanced -> Bridge."
        if pending_bridge > 1:
            return f"Next: review {pending_bridge} pending Bridge commands in Advanced -> Bridge."

        if text == "connected":
            return f"Next: raise the ring and speak. {self._voice_cue_text()}"
        if text in {"listening", "scanning"}:
            return "Next: press the ring button until the light appears."
        if text in {"connecting", "reconnecting"}:
            return "Next: keep the ring close to this computer."
        if text in {"failed", "not found", "reconnect failed"}:
            return "Next: wake the ring, then try Auto Connect Ring again."
        if saved and self.cfg.auto_connect_saved_ring:
            return "Next: press the ring button; the saved-ring listener will connect."
        if saved:
            return "Next: click Auto Connect Ring when the ring light is on."
        return "Next: click Auto Connect Ring, then press the ring button until the light appears."

    def _refresh_quick_ready(self) -> None:
        label = getattr(self, "quick_ready_label", None)
        if label is not None:
            label.setText(self._quick_ready_text())
        next_label = getattr(self, "next_step_label", None)
        if next_label is not None:
            next_label.setText(self._next_step_text())

    def _wake_phrase_config_for_target(target: str) -> tuple[str, str, str] | None:
        return {
            "assistant": ("assistant_wake_word", "Wizpr, Assistant", "Assistant Wake:"),
            "codex": ("codex_wake_word", "Codex", "Codex Wake:"),
            "opencode": ("opencode_wake_word", "OpenCode, Open Code", "OpenCode Wake:"),
            "clipboard": ("clipboard_wake_word", "Wizpr", "Copy Text Wake:"),
            "paste": ("paste_wake_word", "Wizpr", "Voice Keyboard Wake:"),
        }.get(target)

    def _wake_phrase_for_target(self, target: str) -> str:
        item = self._wake_phrase_config_for_target(target)
        if item is None:
            return ""
        attr, default, _label = item
        return str(getattr(self.cfg.transcription, attr, "") or default)

    def _set_wake_phrase_for_target(self, target: str, text: str) -> None:
        item = self._wake_phrase_config_for_target(target)
        if item is None:
            return
        attr, default, _label = item
        value = text.strip() or default
        setattr(self.cfg.transcription, attr, value)
        advanced_attr = {
            "assistant": "transcription_assistant_wake",
            "codex": "transcription_codex_wake",
            "opencode": "transcription_opencode_wake",
            "clipboard": "transcription_clipboard_wake",
            "paste": "transcription_paste_wake",
        }.get(target)
        widget = getattr(self, advanced_attr or "", None)
        if widget is not None:
            widget.blockSignals(True)
            try:
                widget.setText(value)
            finally:
                widget.blockSignals(False)

    def _sync_wake_phrase_ui(self) -> None:
        edit = getattr(self, "wake_phrase_edit", None)
        label = getattr(self, "wake_phrase_label", None)
        if edit is None:
            return
        target = self._ring_voice_target_value()
        item = self._wake_phrase_config_for_target(target)
        edit.blockSignals(True)
        try:
            if item is None:
                if label is not None:
                    label.setText("Wake Phrase:")
                edit.setText("")
                edit.setPlaceholderText("Not used in Transcript Only mode")
                edit.setEnabled(False)
                return

            _attr, default, label_text = item
            if label is not None:
                label.setText(label_text)
            edit.setEnabled(True)
            edit.setPlaceholderText(default)
            edit.setText(self._wake_phrase_for_target(target))
        finally:
            edit.blockSignals(False)

    def _apply_simple_wake_phrase_to_config(self) -> None:
        edit = getattr(self, "wake_phrase_edit", None)
        if edit is None or not edit.isEnabled():
            return
        self._set_wake_phrase_for_target(self._ring_voice_target_value(), edit.text())

    def _simple_wake_phrase_changed(self) -> None:
        edit = getattr(self, "wake_phrase_edit", None)
        if edit is None or not edit.isEnabled():
            return
        self._apply_simple_wake_phrase_to_config()
        save_config(self.app_dir, self.cfg)
        self._sync_wake_phrase_ui()
        self._refresh_quick_ready()
        self.statusBar().showMessage("Wake phrase saved.", 1500)

    def _provider_label(pid: str) -> str:
        return {
            "openai": "OpenAI",
            "ollama": "Ollama",
            "openai_compat": "Compatible Server",
        }.get(pid, pid)

    def _set_active_llm_combo(self, pid: str) -> None:
        combo = getattr(self, "active_llm_combo", None)
        if combo is None:
            return
        idx = combo.findData(pid)
        if idx < 0:
            idx = 0
        combo.blockSignals(True)
        try:
            combo.setCurrentIndex(idx)
        finally:
            combo.blockSignals(False)

    def _active_llm_combo_changed(self) -> None:
        combo = getattr(self, "active_llm_combo", None)
        if combo is None:
            return
        self._on_active_llm_changed(str(combo.currentData() or ""))

    def _provider_talk_button(self, provider_id: str) -> QtWidgets.QPushButton:
        button = QtWidgets.QPushButton("Use for Talk")
        label = self._provider_label(provider_id)
        button.setToolTip(f"Save these settings and use {label} for normal Talk responses.")
        button.clicked.connect(lambda _checked=False, pid=provider_id: self._use_provider_for_talk(pid))
        return button

    def _use_provider_for_talk(self, provider_id: str) -> None:
        save = {
            "openai": self._save_openai,
            "ollama": self._save_ollama,
            "openai_compat": self._save_compat,
        }.get(provider_id)
        if save is not None:
            save()
        self._on_active_llm_changed(provider_id)

    def _show_provider_settings(self) -> None:
        if not self.cfg.show_advanced_options:
            self.advanced_toggle.setChecked(True)
        else:
            self._ensure_advanced_tabs_built()
            self._set_advanced_visible(True)

        if self.llm_tab_index >= 0:
            self.tabs.setCurrentIndex(self.llm_tab_index)
        tabs = getattr(self, "llm_tabs", None)
        if tabs is not None:
            provider_idx = {
                "openai": 0,
                "ollama": 1,
                "openai_compat": 2,
            }.get(self.active_llm_id, 0)
            tabs.setCurrentIndex(provider_idx)

    def _set_combo_data(combo: QtWidgets.QComboBox | None, value: object, fallback: object) -> None:
        if combo is None:
            return
        idx = combo.findData(value)
        if idx < 0:
            idx = combo.findData(fallback)
        combo.blockSignals(True)
        try:
            combo.setCurrentIndex(max(0, idx))
        finally:
            combo.blockSignals(False)

    def _set_check(widget: QtWidgets.QCheckBox | None, checked: bool) -> None:
        if widget is None:
            return
        widget.blockSignals(True)
        try:
            widget.setChecked(bool(checked))
        finally:
            widget.blockSignals(False)

    def _sync_ring_settings_ui(self) -> None:
        voice_mode = (self.cfg.transcription.voice_mode or "proximity").strip().lower()
        sleep_timeout = int(self.cfg.transcription.ring_sleep_timeout_seconds or 0)
        self._set_combo_data(getattr(self, "ring_voice_mode", None), voice_mode, "proximity")
        self._set_combo_data(getattr(self, "transcription_voice_mode", None), voice_mode, "proximity")
        self._set_combo_data(getattr(self, "ring_sleep_timeout", None), sleep_timeout, 5)
        self._set_combo_data(getattr(self, "transcription_sleep_timeout", None), sleep_timeout, 5)
        self._set_combo_data(getattr(self, "ring_button_mode", None), self.cfg.button_mode, "app")
        summary = getattr(self, "ring_button_summary", None)
        if summary is not None:
            summary.setText(self._button_mode_summary())
        self._set_check(getattr(self, "ring_tts_response_check", None), self.cfg.transcription.speak_responses)
        self._set_check(getattr(self, "speak_responses_check", None), self.cfg.transcription.speak_responses)
        self._set_check(getattr(self, "ring_auto_start_check", None), self.cfg.auto_connect_saved_ring)
        self._set_check(getattr(self, "ring_connect_sound_check", None), self.cfg.transcription.ring_connection_sound)
        self._set_check(getattr(self, "ring_mic_sound_check", None), self.cfg.transcription.mic_activation_sound)
        self._set_check(getattr(self, "ring_low_battery_check", None), self.cfg.transcription.low_battery_warning)
        self._set_check(getattr(self, "ring_connection_sound", None), self.cfg.transcription.ring_connection_sound)
        self._set_check(getattr(self, "mic_activation_sound", None), self.cfg.transcription.mic_activation_sound)
        self._set_check(getattr(self, "low_battery_warning", None), self.cfg.transcription.low_battery_warning)
        self._set_check(
            getattr(self, "protect_ring_buttons_check", None),
            self.cfg.protect_connected_ring_buttons,
        )
        self._set_combo_data(
            getattr(self, "interrupt_mode_combo", None),
            self.cfg.transcription.interrupt_mode,
            "word",
        )
        interrupt_edit = getattr(self, "interrupt_word_edit", None)
        if interrupt_edit is not None and not interrupt_edit.hasFocus():
            interrupt_edit.setText(self.cfg.transcription.interrupt_word or "stop")

    def _button_mode_summary(self) -> str:
        mode = (self.cfg.button_mode or "app").strip().lower()
        if mode == "coding":
            return "Buttons: 1 Listen | 2 Last | 3 Codex | Hold LLM"
        if mode == "custom":
            return "Buttons: custom Advanced mappings"
        return "Buttons: 2 New | 3 Edit | connection protected"

    def _simple_ring_settings_changed(self) -> None:
        voice = getattr(self, "ring_voice_mode", None)
        sleep = getattr(self, "ring_sleep_timeout", None)
        button_mode = getattr(self, "ring_button_mode", None)
        tts_response = getattr(self, "ring_tts_response_check", None)
        auto_start = getattr(self, "ring_auto_start_check", None)
        connect_sound = getattr(self, "ring_connect_sound_check", None)
        mic_sound = getattr(self, "ring_mic_sound_check", None)
        low_battery = getattr(self, "ring_low_battery_check", None)
        if voice is not None:
            self.cfg.transcription.voice_mode = str(voice.currentData() or "proximity")
        if sleep is not None:
            self.cfg.transcription.ring_sleep_timeout_seconds = int(sleep.currentData() or 0)
        if button_mode is not None:
            self.cfg.button_mode = str(button_mode.currentData() or "app")
            if self.cfg.button_mode in BUTTON_MODE_MAPPINGS:
                _sync_button_mode_mappings(self.cfg)
        if tts_response is not None:
            self.cfg.transcription.speak_responses = bool(tts_response.isChecked())
            if not self.cfg.transcription.speak_responses:
                self._stop_speech(show_status=False)
        if auto_start is not None:
            self.cfg.auto_connect_saved_ring = bool(auto_start.isChecked())
        if connect_sound is not None:
            self.cfg.transcription.ring_connection_sound = bool(connect_sound.isChecked())
        if mic_sound is not None:
            self.cfg.transcription.mic_activation_sound = bool(mic_sound.isChecked())
        if low_battery is not None:
            self.cfg.transcription.low_battery_warning = bool(low_battery.isChecked())
        save_config(self.app_dir, self.cfg)
        if auto_start is not None:
            if self.cfg.auto_connect_saved_ring:
                self._ring_manual_disconnect = False
                self._schedule_saved_ring_auto_connect()
            else:
                self._cancel_saved_ring_auto_connect()
                client = self.ble.client
                if client is None or not client.is_connected:
                    self._ring_keep_connected = False
                    if self._remembered_ring_address():
                        self._set_ring_connection_status("Remembered", "neutral")
                    else:
                        self._set_ring_connection_status("Disconnected", "disconnected")
        self._sync_ring_settings_ui()
        self._reload_mapping_table()
        self._refresh_quick_ready()
        self.statusBar().showMessage("Ring settings saved.", 1500)

    def _sync_wake_required_ui(self) -> None:
        target = self._ring_voice_target_value()
        always_required = self._target_always_requires_wake_phrase(target)
        for attr in ("wake_required_check", "transcription_require_wake"):
            widget = getattr(self, attr, None)
            if widget is None:
                continue
            if attr == "wake_required_check":
                checked = self._target_requires_wake_phrase(target)
                enabled = not always_required
                tip = (
                    "This target always requires its wake phrase before running."
                    if always_required
                    else "Require the selected wake phrase before ring audio is sent automatically."
                )
            else:
                checked = bool(self.cfg.transcription.require_wake_word)
                enabled = True
                tip = "Require the Assistant wake phrase before normal assistant voice sends."
            widget.blockSignals(True)
            try:
                widget.setChecked(checked)
                widget.setEnabled(enabled)
                widget.setToolTip(tip)
            finally:
                widget.blockSignals(False)

    def _wake_required_changed(self, checked: bool) -> None:
        self.cfg.transcription.require_wake_word = bool(checked)
        self._sync_wake_required_ui()
        self._save_transcription()
        self._set_voice_status(self._wake_required_status_text())
        if self._target_requires_wake_phrase(self._ring_voice_target_value()):
            self.statusBar().showMessage("Wake phrase required for automatic voice sends.", 2500)
        else:
            self.statusBar().showMessage("Wake phrase requirement off.", 2500)
        self._refresh_quick_ready()

    def _set_ring_voice_target(self, target: str) -> None:
        target = target.strip().lower()
        if target not in {"assistant", "codex", "opencode", "transcript", "clipboard", "paste"}:
            target = "assistant"

        mappings = self.cfg.mappings or {}
        for action in (
            "send_audio_to_assistant",
            "send_audio_to_codex",
            "send_audio_to_opencode",
            "transcribe_audio_only",
            "copy_audio_to_clipboard",
            "paste_audio_to_active_app",
            "copy_last_transcript",
            "paste_last_transcript",
        ):
            mappings.setdefault(action, [])
            mappings[action] = [topic for topic in mappings[action] if topic != "audio_capture"]

        action_by_target = {
            "assistant": "send_audio_to_assistant",
            "codex": "send_audio_to_codex",
            "opencode": "send_audio_to_opencode",
            "transcript": "transcribe_audio_only",
            "clipboard": "copy_audio_to_clipboard",
            "paste": "paste_audio_to_active_app",
        }
        mappings[action_by_target[target]].append("audio_capture")
        self.cfg.mappings = mappings
        self.cfg.ring_voice_target = target
        save_config(self.app_dir, self.cfg)
        self._sync_ring_voice_target_ui()
        self._sync_wake_required_ui()
        self._sync_wake_phrase_ui()
        self._reload_mapping_table()
        self._refresh_quick_ready()

        label = {
            "assistant": "Assistant",
            "codex": "Codex",
            "opencode": "OpenCode",
            "transcript": "Transcript Only",
            "clipboard": "Copy Text",
            "paste": "Voice Keyboard",
        }[target]
        suffix = " (wake phrase required)" if self._target_requires_wake_phrase(target) else ""
        self._set_voice_status(f"Voice target: {label}{suffix}")
        self.statusBar().showMessage(f"Ring voice target: {label}.", 2500)

    def _reload_mapping_table(self) -> None:
        if not hasattr(self, "map_table"):
            return
        self.map_table.setRowCount(0)
        for action, triggers in (self.cfg.mappings or {}).items():
            for trig in triggers:
                r = self.map_table.rowCount()
                self.map_table.insertRow(r)
                self.map_table.setItem(r, 0, QtWidgets.QTableWidgetItem(str(trig)))
                self.map_table.setItem(r, 1, QtWidgets.QTableWidgetItem(str(action)))

    def _add_mapping(self) -> None:
        trig = self.map_trigger.text().strip()
        action = self.map_action.currentText().strip()
        if not trig:
            self.statusBar().showMessage("Trigger is required.", 2000)
            return
        self.cfg.mappings.setdefault(action, [])
        if trig not in self.cfg.mappings[action]:
            self.cfg.mappings[action].append(trig)
        if trig in BUTTON_TOPICS:
            self.cfg.button_mode = "custom"
        save_config(self.app_dir, self.cfg)
        self._reload_mapping_table()
        self._sync_ring_settings_ui()
        self.statusBar().showMessage("Mapping added.", 1500)

    def _remove_mapping(self) -> None:
        rows = sorted({i.row() for i in self.map_table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        button_mapping_changed = False
        for r in rows:
            trig = self.map_table.item(r, 0).text()
            action = self.map_table.item(r, 1).text()
            if action in (self.cfg.mappings or {}) and trig in self.cfg.mappings[action]:
                self.cfg.mappings[action].remove(trig)
                if trig in BUTTON_TOPICS:
                    button_mapping_changed = True
        if button_mapping_changed:
            self.cfg.button_mode = "custom"
        save_config(self.app_dir, self.cfg)
        self._reload_mapping_table()
        self._sync_ring_settings_ui()
        self.statusBar().showMessage("Mapping removed.", 1500)

    def _save_mobile_bridge(self) -> None:
        if hasattr(self, "bridge_enabled"):
            self.cfg.mobile_bridge.enabled = bool(self.bridge_enabled.isChecked())
        if hasattr(self, "bridge_host"):
            self.cfg.mobile_bridge.host = self.bridge_host.text().strip() or "127.0.0.1"
        if hasattr(self, "bridge_port"):
            self.cfg.mobile_bridge.port = int(self.bridge_port.value())
        if hasattr(self, "bridge_token"):
            self.cfg.mobile_bridge.token = self.bridge_token.text().strip()
        if hasattr(self, "bridge_require_approval"):
            self.cfg.mobile_bridge.require_approval = bool(self.bridge_require_approval.isChecked())
        if bridge_needs_token(self.cfg.mobile_bridge) and not (self.cfg.mobile_bridge.token or "").strip():
            self.cfg.mobile_bridge.token = make_bridge_token()
            if hasattr(self, "bridge_token"):
                self.bridge_token.setText(self.cfg.mobile_bridge.token)
        save_config(self.app_dir, self.cfg)
        self._set_mobile_bridge_status()

    def _generate_mobile_bridge_token(self) -> None:
        self.bridge_token.setText(make_bridge_token())
        self._save_mobile_bridge()
        self.statusBar().showMessage("Bridge token generated.", 2000)

    def _mobile_bridge_page_url(self) -> str:
        return bridge_app_url(self.cfg.mobile_bridge, include_token=True)

    def _copy_mobile_bridge_url(self) -> None:
        self._save_mobile_bridge()
        QtWidgets.QApplication.clipboard().setText(self._mobile_bridge_page_url())
        self.statusBar().showMessage("Bridge page URL copied.", 1500)

    def _open_mobile_bridge_page(self) -> None:
        async def _run() -> None:
            if not self.mobile_bridge.running:
                await self._start_mobile_bridge(show_status=False)
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(self._mobile_bridge_page_url()))
            self.statusBar().showMessage("Bridge page opened.", 1500)

        self.loop.create_task(_run())

    def _start_mobile_bridge_clicked(self) -> None:
        self.loop.create_task(self._start_mobile_bridge())

    async def _start_mobile_bridge(self, show_status: bool = True) -> None:
        self._save_mobile_bridge()
        if hasattr(self, "bridge_start"):
            self.bridge_start.setEnabled(False)
        try:
            url = await self.mobile_bridge.start()
            self._set_mobile_bridge_status()
            if show_status:
                self.statusBar().showMessage(f"Bridge running: {url}", 4000)
        except Exception as exc:
            if hasattr(self, "bridge_status"):
                self.bridge_status.setText(f"Failed: {exc}")
            if show_status:
                self.statusBar().showMessage(f"Bridge failed: {exc}", 7000)
        finally:
            if hasattr(self, "bridge_start"):
                self.bridge_start.setEnabled(True)
            self._set_mobile_bridge_status()

    def _stop_mobile_bridge_clicked(self) -> None:
        self.loop.create_task(self._stop_mobile_bridge())

    async def _stop_mobile_bridge(self) -> None:
        if hasattr(self, "bridge_stop"):
            self.bridge_stop.setEnabled(False)
        try:
            await self.mobile_bridge.stop()
            self._set_mobile_bridge_status()
            self.statusBar().showMessage("Bridge stopped.", 2500)
        finally:
            if hasattr(self, "bridge_stop"):
                self.bridge_stop.setEnabled(True)
            self._set_mobile_bridge_status()

    def _set_mobile_bridge_status(self) -> None:
        if not hasattr(self, "bridge_status"):
            return
        url = bridge_url(self.cfg.mobile_bridge)
        if self.mobile_bridge.running:
            text = f"Running at {url}"
        else:
            text = f"Stopped ({url})"
        self.bridge_status.setText(text)
        app_url = bridge_app_url(self.cfg.mobile_bridge, include_token=True)
        self.bridge_status.setToolTip(bridge_app_url(self.cfg.mobile_bridge, include_token=False))
        if hasattr(self, "bridge_phone_url"):
            self.bridge_phone_url.setText(app_url)
        if hasattr(self, "bridge_stop"):
            self.bridge_stop.setEnabled(self.mobile_bridge.running)
        self._refresh_quick_ready()

    async def _on_mobile_bridge_request(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        request_id = str(payload.get("id") or "").strip()
        if not request_id:
            return
        self._bridge_pending_by_id[request_id] = payload
        self._reload_bridge_pending_table()
        self._refresh_quick_ready()
        self.statusBar().showMessage("Bridge command waiting for approval.", 5000)
        self._append_ble_log(f"bridge_request: {payload}")

    def _reload_bridge_pending_table(self) -> None:
        table = getattr(self, "bridge_pending", None)
        if table is None:
            return
        table.setRowCount(0)
        for request_id, payload in self._bridge_pending_by_id.items():
            row = table.rowCount()
            table.insertRow(row)
            target_item = QtWidgets.QTableWidgetItem(str(payload.get("target") or "assistant"))
            target_item.setData(QtCore.Qt.UserRole, request_id)
            table.setItem(row, 0, target_item)
            table.setItem(row, 1, QtWidgets.QTableWidgetItem(str(payload.get("source") or "mobile")))
            table.setItem(row, 2, QtWidgets.QTableWidgetItem(str(payload.get("text") or "")))
        table.resizeColumnsToContents()

    def _selected_bridge_request_id(self) -> str:
        rows = {i.row() for i in self.bridge_pending.selectedIndexes()}
        if not rows:
            return ""
        item = self.bridge_pending.item(sorted(rows)[0], 0)
        if item is None:
            return ""
        return str(item.data(QtCore.Qt.UserRole) or "")

    def _remove_bridge_request_row(self, request_id: str) -> None:
        if not hasattr(self, "bridge_pending"):
            return
        for row in range(self.bridge_pending.rowCount()):
            item = self.bridge_pending.item(row, 0)
            if item is not None and str(item.data(QtCore.Qt.UserRole) or "") == request_id:
                self.bridge_pending.removeRow(row)
                return

    def _approve_mobile_bridge_request(self) -> None:
        request_id = self._selected_bridge_request_id()
        if not request_id:
            self.statusBar().showMessage("Select a bridge request first.", 2500)
            return

        async def _run() -> None:
            request = self.mobile_bridge.take_pending(request_id) or self._bridge_pending_by_id.get(request_id)
            if not request:
                self.statusBar().showMessage("Bridge request was not found.", 2500)
                return
            result = await self._handle_mobile_bridge_command(request)
            self._bridge_pending_by_id.pop(request_id, None)
            self._remove_bridge_request_row(request_id)
            self._refresh_quick_ready()
            if self.mobile_bridge.running:
                await self.mobile_bridge.publish_event(
                    "bridge_command_result",
                    {"id": request_id, "target": request.get("target"), **result},
                )
            self.statusBar().showMessage("Bridge command handled.", 3000)

        self.loop.create_task(_run())

    def _reject_mobile_bridge_request(self) -> None:
        request_id = self._selected_bridge_request_id()
        if not request_id:
            self.statusBar().showMessage("Select a bridge request first.", 2500)
            return
        self.mobile_bridge.take_pending(request_id)
        self._bridge_pending_by_id.pop(request_id, None)
        self._remove_bridge_request_row(request_id)
        self._refresh_quick_ready()
        self.loop.create_task(
            self.mobile_bridge.publish_event(
                "bridge_command_result",
                {"id": request_id, "ok": False, "error": "rejected"},
            )
        )
        self.statusBar().showMessage("Bridge command rejected.", 2500)

    async def _handle_mobile_bridge_command(self, request: dict[str, Any]) -> dict[str, Any]:
        text = str(request.get("text") or "").strip()
        target = str(request.get("target") or "assistant").strip().lower()
        if not text:
            return {"ok": False, "error": "empty command"}

        if target == "assistant":
            await self._send_prompt_to_assistant(text)
        elif target == "codex":
            await self._send_prompt_to_codex(text)
        elif target == "opencode":
            await self._send_prompt_to_opencode(text)
        elif target == "transcript":
            self._last_transcript = text
            self.prompt.setPlainText(text)
            self._update_talk_action_state()
        elif target == "clipboard":
            self._last_transcript = text
            self.prompt.setPlainText(text)
            self._update_talk_action_state()
            if not self._copy_text_to_clipboard(text, "Bridge text"):
                return {"ok": False, "error": "no text copied"}
            return {"ok": True, "text": "Copied to clipboard."}
        elif target == "paste":
            self._last_transcript = text
            self.prompt.setPlainText(text)
            self._update_talk_action_state()
            if not self._copy_text_to_clipboard(text, "Bridge text"):
                return {"ok": False, "error": "no text copied"}
            try:
                await self._send_paste_hotkey()
            except Exception as exc:
                self.statusBar().showMessage(f"Bridge paste failed: {exc}", 5000)
                self.output.appendPlainText(f"[bridge paste error] {exc}\n")
                return {"ok": False, "error": str(exc)}
            self.statusBar().showMessage("Bridge text pasted into the active app.", 2500)
            return {"ok": True, "text": "Pasted into active app."}
        else:
            return {"ok": False, "error": f"unknown target: {target}"}
        return {"ok": True}

    def _set_ring_connection_status(self, text: str, state: str = "neutral") -> None:
        self._ring_connection_text = str(text or "Disconnected")
        self._ring_connection_state = str(state or "neutral")
        colors = {
            "connected": ("#143d24", "#9ff2b5"),
            "connecting": ("#3a3212", "#ffd76a"),
            "scanning": ("#173452", "#9ed0ff"),
            "disconnected": ("#3d1f1f", "#ffb4b4"),
            "error": ("#4a1717", "#ff9f9f"),
            "neutral": ("#2b2f36", "#d9e1ec"),
        }
        bg, fg = colors.get(self._ring_connection_state, colors["neutral"])
        label = getattr(self, "ring_connection_status", None)
        if label is not None:
            label.setText(self._ring_connection_text)
            label.setStyleSheet(
                f"QLabel {{ background: {bg}; color: {fg}; border: 1px solid {fg}; "
                "border-radius: 4px; padding: 3px 8px; font-weight: 600; }"
            )
        sidebar = getattr(self, "sidebar_ring_status", None)
        if sidebar is not None:
            sidebar.setText(f"Ring {self._ring_connection_text}")
            sidebar.setProperty("connected", self._ring_connection_state == "connected")
            sidebar.style().unpolish(sidebar)
            sidebar.style().polish(sidebar)
        self._refresh_mic_button_state()
        self._update_ring_action_buttons()
        if self._ring_connection_state == "disconnected" and hasattr(self, "ring_last_event"):
            self.ring_last_event.setText("Last event: disconnected")
        elif self._ring_connection_state == "error" and hasattr(self, "ring_last_event"):
            self.ring_last_event.setText("Last event: connection failed")
        self._refresh_quick_ready()

    def _update_ring_action_buttons(self) -> None:
        status = getattr(self, "ring_connection_status", None)
        text = status.text().strip().casefold() if status is not None else ""
        has_client = bool(getattr(self.ble, "client", None))
        busy = bool(getattr(self, "_ring_connecting", False))
        keep_connected = bool(getattr(self, "_ring_keep_connected", False))
        startup_waiting = self._saved_ring_auto_connect_running()
        waiting_states = {"connected", "connecting", "reconnecting", "scanning", "waiting", "listening"}
        disconnect_enabled = has_client or keep_connected or busy or startup_waiting or text in waiting_states
        auto_enabled = not (has_client or busy or startup_waiting or text in waiting_states)
        self._set_optional_button_enabled("wizpr_auto_btn", auto_enabled)
        self._set_optional_button_enabled("ble_disconnect_btn", disconnect_enabled)

    def _append_ble_log(self, text: str) -> None:
        line = str(text)
        box = getattr(self, "notify_box", None)
        if box is not None:
            box.appendPlainText(line)
            return
        self._ble_log_backlog.append(line)
        if len(self._ble_log_backlog) > 200:
            del self._ble_log_backlog[: len(self._ble_log_backlog) - 200]

    def _set_optional_button_enabled(self, attr: str, enabled: bool) -> None:
        button = getattr(self, attr, None)
        if button is not None:
            button.setEnabled(enabled)

    def _advanced_scan_seconds(self) -> float:
        spin = getattr(self, "ble_scan_seconds", None)
        if spin is None:
            return 60.0
        return max(1.0, float(spin.value()))

    def _scan_ble(self) -> None:
        self._ensure_advanced_ble_built()

        async def _run():
            self.ble_scan_btn.setEnabled(False)
            try:
                secs = float(self.ble_scan_seconds.value())
                self.statusBar().showMessage(f"Scanning BLE ({secs:.0f}s)...", 2000)
                devs = await self.ble.scan(seconds=secs)
                self._fill_ble_table(devs)
                self._append_scan_details(devs)
                self.statusBar().showMessage(f"Found {len(devs)} device(s).", 2500)
            except Exception as e:
                logger.exception("BLE scan failed")
                self._append_ble_log(f"scan_failed: {e}")
                self.statusBar().showMessage(f"Scan failed: {e}", 4000)
            finally:
                self.ble_scan_btn.setEnabled(True)
        self.loop.create_task(_run())

    async def _ble_health_blocks_wizpr(self) -> bool:
        report = await self.ble.health_report()
        if report.get("adapter_has_ble_central") is False:
            self._append_ble_log(BLEManager.format_health_report(report))
            self.statusBar().showMessage("BLE blocked: active Windows radio does not expose BLE Central/GATT.", 9000)
            return True
        return False

    def _load_windows_ble_devices(self) -> None:
        self._ensure_advanced_ble_built()

        async def _run():
            self.ble_windows_btn.setEnabled(False)
            try:
                devs = await self.ble.windows_associated_devices()
                self._fill_ble_table(devs)
                self._append_scan_details(devs)
                ring_count = sum(1 for d in devs if d.candidate_label == "WIZPR ring")
                case_count = sum(1 for d in devs if d.candidate_label == "WIZPR case")
                ble_client_count = sum(1 for d in devs if d.candidate_label == "WIZPR BLE client")
                self._append_ble_log(
                    f"windows_devices: loaded {len(devs)} Windows Bluetooth LE device(s), "
                    f"{ring_count} WIZPR ring candidate(s), {case_count} WIZPR case device(s), "
                    f"{ble_client_count} WIZPR BLE client device(s)."
                )
                self.statusBar().showMessage(f"Loaded {len(devs)} Windows Bluetooth LE device(s).", 3000)
            except Exception as e:
                logger.exception("Windows Bluetooth device load failed")
                self._append_ble_log(f"windows_devices_failed: {e}")
                self.statusBar().showMessage(f"Windows device load failed: {e}", 5000)
            finally:
                self.ble_windows_btn.setEnabled(True)
        self.loop.create_task(_run())

    def _ble_doctor(self) -> None:
        self._ensure_advanced_ble_built()

        async def _run():
            self.ble_doctor_btn.setEnabled(False)
            try:
                report = await self.ble.health_report()
                self._append_ble_log(BLEManager.format_health_report(report))
                if report.get("adapter_has_ble_central") is False:
                    self.statusBar().showMessage("BLE Doctor: active Windows radio does not expose BLE Central/GATT.", 9000)
                elif not report.get("scanner_ok"):
                    self.statusBar().showMessage("BLE Doctor: live BLE scanner cannot start.", 9000)
                elif not report.get("ring_candidates"):
                    self.statusBar().showMessage("BLE Doctor: BLE looks usable, but no WIZPR ring is known yet.", 7000)
                else:
                    self.statusBar().showMessage("BLE Doctor: WIZPR ring candidate is known to Windows.", 7000)
            except Exception as e:
                logger.exception("BLE Doctor failed")
                self._append_ble_log(f"ble_doctor_failed: {e}")
                self.statusBar().showMessage(f"BLE Doctor failed: {e}", 6000)
            finally:
                self.ble_doctor_btn.setEnabled(True)
        self.loop.create_task(_run())

    def _use_manual_ble_address(self) -> None:
        self._ensure_advanced_ble_built()
        raw = self.ble_manual_address.text().strip()
        clean = re.sub(r"[^0-9a-fA-F]", "", raw)
        if len(clean) != 12:
            self.statusBar().showMessage("Enter a 12-digit BLE address, e.g. AA:BB:CC:DD:EE:FF.", 4000)
            return
        address = ":".join(clean.upper()[i : i + 2] for i in range(0, 12, 2))
        dev = DiscoveredDevice(address=address, name="Manual BLE address", rssi=0, service_uuids=[])
        self._fill_ble_table([dev])
        self.ble_table.selectRow(0)
        self._append_ble_log(f"manual_address: loaded {address}; click Connect to try it.")
        self.statusBar().showMessage(f"Manual BLE address loaded: {address}", 3000)

    def _fill_ble_table(self, devs: list[DiscoveredDevice]) -> None:
        table = getattr(self, "ble_table", None)
        if table is None:
            return
        self.ble_table.setRowCount(0)
        for d in devs:
            r = self.ble_table.rowCount()
            self.ble_table.insertRow(r)
            self.ble_table.setItem(r, 0, QtWidgets.QTableWidgetItem(d.candidate_label))
            self.ble_table.setItem(r, 1, QtWidgets.QTableWidgetItem(d.name or "(no name)"))
            self.ble_table.setItem(r, 2, QtWidgets.QTableWidgetItem(d.address))
            self.ble_table.setItem(r, 3, QtWidgets.QTableWidgetItem(str(d.rssi)))
            services = ", ".join((d.service_uuids or [])[:3])
            if d.service_uuids and len(d.service_uuids) > 3:
                services += ", ..."
            mfg = "; ".join(f"{k}:{v}" for k, v in (d.manufacturer_data or {}).items())
            svc = "; ".join(f"{k}:{v}" for k, v in (d.service_data or {}).items())
            detail_bits = []
            if d.tx_power is not None:
                detail_bits.append(f"TX {d.tx_power}")
            if services:
                detail_bits.append(f"services: {services}")
            if mfg:
                detail_bits.append("mfg data")
            if svc:
                detail_bits.append("service data")
            detail = "; ".join(detail_bits)
            detail_item = QtWidgets.QTableWidgetItem(detail[:90] + ("..." if len(detail) > 90 else ""))
            detail_item.setToolTip(
                "\n".join(
                    [
                        f"Services: {', '.join(d.service_uuids or []) or '-'}",
                        f"Manufacturer: {mfg or '-'}",
                        f"Service data: {svc or '-'}",
                    ]
                )
            )
            self.ble_table.setItem(r, 4, detail_item)

        self.ble_table.resizeColumnsToContents()

    def _append_scan_details(self, devs: list[DiscoveredDevice]) -> None:
        lines = ["scan_details:"]
        for d in devs:
            services = ", ".join(d.service_uuids or []) or "-"
            mfg = "; ".join(f"{k}:{v}" for k, v in (d.manufacturer_data or {}).items()) or "-"
            service_data = "; ".join(f"{k}:{v}" for k, v in (d.service_data or {}).items()) or "-"
            tx = d.tx_power if d.tx_power is not None else "-"
            lines.append(
                f"  {d.candidate_label}: {d.name or '(no name)'} [{d.address}] RSSI={d.rssi} TX={tx} "
                f"services={services} mfg={mfg} service_data={service_data}"
            )
        self._append_ble_log("\n".join(lines))

    def _selected_ble_address(self) -> str:
        row = self._selected_ble_row()
        if row < 0:
            return ""
        item = self.ble_table.item(row, 2)
        return item.text().strip() if item else ""

    def _selected_ble_row(self) -> int:
        table = getattr(self, "ble_table", None)
        if table is None:
            return -1
        rows = {i.row() for i in self.ble_table.selectedIndexes()}
        return sorted(rows)[0] if rows else -1

    def _selected_ble_looks_like_case(self) -> bool:
        row = self._selected_ble_row()
        if row < 0:
            return False
        label = (self.ble_table.item(row, 0).text() if self.ble_table.item(row, 0) else "").casefold()
        name = (self.ble_table.item(row, 1).text() if self.ble_table.item(row, 1) else "").casefold()
        services = (self.ble_table.item(row, 4).toolTip() if self.ble_table.item(row, 4) else "").casefold()
        return "wizpr case" in label or "wizpr case" in name or "df429eb3ad11" in services

    def _selected_ble_candidate_label(self) -> str:
        row = self._selected_ble_row()
        if row < 0:
            return ""
        item = self.ble_table.item(row, 0)
        return item.text().strip() if item else ""

    def _device_advertises_wizpr_ring(self, dev: DiscoveredDevice) -> bool:
        services = {str(u).casefold() for u in (dev.service_uuids or [])}
        service_data = {str(u).casefold() for u in (dev.service_data or {}).keys()}
        return WIZPR_RING_SERVICE_UUID in services or WIZPR_RING_SERVICE_UUID in service_data

    def _remembered_ring_address(self) -> str:
        return (self.cfg.last_ble_address or self.ring_profile.address or "").strip()

    def _update_saved_ring_status(self) -> None:
        label = getattr(self, "ring_saved_status", None)
        if label is None:
            return
        address = (self.cfg.last_ble_address or "").strip()
        label.setText(f"Saved ring: {address}" if address else "Saved ring: none")
        label.setToolTip(address)
        button = getattr(self, "forget_ring_btn", None)
        if button is not None:
            button.setEnabled(bool(address))
        self._refresh_quick_ready()

    def _forget_saved_ring(self) -> None:
        self._cancel_saved_ring_auto_connect()
        self.cfg.last_ble_address = ""
        self.ring_profile.address = ""
        self._ring_keep_connected = False
        save_config(self.app_dir, self.cfg)
        self._update_saved_ring_status()
        if self.ble.client is None:
            self._set_ring_connection_status("Disconnected", "disconnected")
            self.ring_connection_status.setToolTip("")
        self._append_ring_activity("Saved ring forgotten.")
        self.statusBar().showMessage("Saved ring forgotten.", 2500)

    def _choose_auto_ring_candidate(self, devices: list[DiscoveredDevice]) -> DiscoveredDevice | None:
        remembered = self._remembered_ring_address().casefold()
        rings = [dev for dev in devices if dev.candidate_label == "WIZPR ring"]
        if not rings:
            return None

        if remembered:
            remembered_matches = [
                dev for dev in rings
                if dev.address.casefold() == remembered
            ]
            if remembered_matches:
                return remembered_matches[0]

        with_service = [dev for dev in rings if self._device_advertises_wizpr_ring(dev)]
        return sorted(with_service or rings, key=BLEManager._device_sort_key)[0]

    def _mark_ring_connected(self, address: str) -> None:
        self.cfg.last_ble_address = address
        self.ring_profile.address = address
        self._ring_keep_connected = True
        self._ring_manual_disconnect = False
        save_config(self.app_dir, self.cfg)
        self._set_ring_connection_status("Connected", "connected")
        self.ring_connection_status.setToolTip(address)
        self._update_saved_ring_status()
        self.ring_last_event.setText("Last event: connected")
        self._append_ring_activity(f"Connected: {address}")
        if self.cfg.transcription.ring_connection_sound:
            self._play_feedback_sound("connect")
        self._start_ring_keepalive()
        self._warm_transcriber_after_ring_connect()

    def _cancel_ring_background_tasks(self) -> list[asyncio.Task[Any]]:
        canceled: list[asyncio.Task[Any]] = []
        for attr in ("_startup_auto_connect_task", "_ring_reconnect_task", "_ring_keepalive_task", "_ring_sleep_timeout_task"):
            task = getattr(self, attr, None)
            if task is not None and not task.done():
                task.cancel()
                canceled.append(task)
            setattr(self, attr, None)
        return canceled

    def _saved_ring_auto_connect_running(self) -> bool:
        task = getattr(self, "_startup_auto_connect_task", None)
        return task is not None and not task.done()

    def _cancel_saved_ring_auto_connect(self) -> None:
        task = getattr(self, "_startup_auto_connect_task", None)
        if task is not None and not task.done():
            task.cancel()
        self._startup_auto_connect_task = None
        self._update_ring_action_buttons()

    def _schedule_saved_ring_auto_connect(self) -> None:
        if not self.cfg.auto_connect_saved_ring:
            return
        if not self._remembered_ring_address():
            return
        if self._ring_manual_disconnect or self._ring_connecting:
            return
        client = self.ble.client
        if client is not None and client.is_connected:
            return
        task = self._startup_auto_connect_task
        if task is not None and not task.done():
            return
        self._startup_auto_connect_task = self.loop.create_task(self._connect_saved_ring_at_startup())
        self._update_ring_action_buttons()

    async def _connect_saved_ring_at_startup(self) -> None:
        first_address = self._remembered_ring_address()
        if not first_address or not self.cfg.auto_connect_saved_ring:
            return
        self._ring_keep_connected = True
        self._ring_manual_disconnect = False
        self._set_ring_connection_status("Listening", "scanning")
        self.ring_connection_status.setToolTip(first_address)
        self._append_ring_activity("Listening for saved ring. Press the ring button until the light appears.")
        self.statusBar().showMessage("Listening for saved WIZPR Ring. Press the ring button until the light appears.", 7000)

        try:
            first_pass = True
            while self.cfg.auto_connect_saved_ring:
                address = self._remembered_ring_address()
                if not address or self._ring_manual_disconnect or not self._ring_keep_connected:
                    return

                self._set_ring_connection_status("Listening", "scanning")
                self.ring_connection_status.setToolTip(address)
                timeout = SAVED_RING_STARTUP_SCAN_SECONDS if first_pass else SAVED_RING_RETRY_SCAN_SECONDS
                dev = await self._scan_for_saved_ring(address, timeout=timeout)
                if self._ring_manual_disconnect or not self._ring_keep_connected:
                    return
                if dev is not None:
                    self._set_ring_connection_status("Connecting", "connecting")
                    if await self._connect_remembered_ring(dev.address, timeout=18.0, quick=True):
                        self.statusBar().showMessage(f"Connected saved ring: {dev.name or dev.address}", 4000)
                        return

                self._append_ble_log(f"startup_auto_connect: saved ring advertisement not seen; trying cached address {address}.")
                self._set_ring_connection_status("Connecting", "connecting")
                if await self._connect_remembered_ring(address, timeout=12.0, quick=False):
                    self.statusBar().showMessage(f"Connected saved ring: {address}", 4000)
                    return

                if self._ring_manual_disconnect or not self._ring_keep_connected:
                    return
                self._set_ring_connection_status("Listening", "scanning")
                self.ring_connection_status.setToolTip(address)
                self._append_ring_activity("Still listening for saved ring. Press the ring button until the light appears.")
                self.statusBar().showMessage("Still listening for saved WIZPR Ring. Press Disconnect to stop.", 6000)
                first_pass = False
                await asyncio.sleep(SAVED_RING_RETRY_DELAY_SECONDS)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("Startup saved-ring auto-connect failed: %s", exc)
            if not self._ring_manual_disconnect:
                self._ring_keep_connected = False
                self._set_ring_connection_status("Remembered", "neutral")
                self.ring_connection_status.setToolTip(self._remembered_ring_address())
                self._append_ring_activity(f"Startup saved-ring connect failed: {exc}")
        finally:
            self._startup_auto_connect_task = None
            self._update_ring_action_buttons()

    def _start_ring_keepalive(self) -> None:
        task = self._ring_keepalive_task
        if task is not None and not task.done():
            return
        self._ring_keepalive_task = self.loop.create_task(self._ring_keepalive_loop())

    def _warm_transcriber_after_ring_connect(self) -> None:
        if not self.cfg.transcription.warm_after_connect:
            return
        if self._transcription_backend() != "local":
            return
        if not local_transcription_uses_persistent_worker():
            return
        if self._warm_task is not None and not self._warm_task.done():
            return
        model = self.cfg.transcription.local_model or "small.en"
        compute = self.cfg.transcription.local_compute_type or "int8"

        async def _run() -> None:
            self._set_voice_status(f"Voice: warming local STT {model}")
            self.statusBar().showMessage(f"Warming voice model: {model}...", 4000)
            err = await warm_local_transcriber(model_name=model, compute_type=compute)
            if err:
                logger.warning("Local transcription warmup after ring connect failed: %s", err)
                self._set_voice_status("Voice: local STT warmup failed")
                return
            self._set_voice_status(f"Voice ready: local STT {model}")
            self.statusBar().showMessage(f"Voice model ready: {model}", 3000)

        self._warm_task = self.loop.create_task(_run())

    async def _ring_keepalive_loop(self) -> None:
        next_battery_check = asyncio.get_running_loop().time() + 300.0
        while self._ring_keep_connected:
            await asyncio.sleep(15.0)
            if not self._ring_keep_connected:
                return
            client = self.ble.client
            if client is None or not client.is_connected:
                await self._handle_unexpected_ring_disconnect(self._remembered_ring_address())
                return
            if asyncio.get_running_loop().time() < next_battery_check:
                continue
            try:
                await self.ring.query_battery()
                next_battery_check = asyncio.get_running_loop().time() + 300.0
            except Exception as exc:
                logger.warning("WIZPR keepalive failed: %s", exc)
                await self._handle_unexpected_ring_disconnect(self._remembered_ring_address())
                return

    def _start_ring_sleep_timeout_guard(self) -> None:
        self._cancel_ring_sleep_timeout_guard()
        if (self.cfg.transcription.voice_mode or "proximity").strip().lower() != "proximity":
            return
        timeout = int(self.cfg.transcription.ring_sleep_timeout_seconds or 0)
        if timeout <= 0:
            return
        start_packets = self.ring.audio.packet_count

        async def _run() -> None:
            try:
                await asyncio.sleep(float(timeout))
                client = self.ble.client
                if client is None or not client.is_connected:
                    return
                if self.ring.audio.packet_count > start_packets:
                    return
                await self.ring.sleep()
                self._append_ble_log(f"sleep_timeout: SLEEP sent after {timeout}s with no audio packets.")
                self._append_ring_activity("Sleep sent after timeout.")
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("WIZPR sleep timeout guard failed: %s", exc)

        self._ring_sleep_timeout_task = self.loop.create_task(_run())

    def _cancel_ring_sleep_timeout_guard(self) -> None:
        task = self._ring_sleep_timeout_task
        self._ring_sleep_timeout_task = None
        if task is not None and not task.done():
            task.cancel()

    def _on_ble_disconnected(self, address: str) -> None:
        if self.loop.is_closed():
            return

        def _schedule() -> None:
            self._cancel_ring_sleep_timeout_guard()
            if self._ring_connecting or self._ring_manual_disconnect or not self._ring_keep_connected:
                return
            if self._ring_reconnect_task is None or self._ring_reconnect_task.done():
                self._ring_reconnect_task = self.loop.create_task(self._handle_unexpected_ring_disconnect(address))

        try:
            self.loop.call_soon_threadsafe(_schedule)
        except RuntimeError:
            pass

    async def _handle_unexpected_ring_disconnect(self, address: str) -> None:
        if self._ring_manual_disconnect or not self._ring_keep_connected:
            return
        addr = address or self._remembered_ring_address()
        if not addr:
            self._set_ring_connection_status("Disconnected", "disconnected")
            return
        self._set_ring_connection_status("Reconnecting", "connecting")
        self.statusBar().showMessage("Ring disconnected; scanning for saved ring...", 4000)
        self._append_ring_activity("Disconnected; scanning for saved ring.")
        await asyncio.sleep(2.0)
        if self._ring_manual_disconnect or not self._ring_keep_connected:
            return

        self._append_ble_log(
            "reconnect: scanning up to 25s for the saved WIZPR ring. "
            "Press the ring button until the light appears if it does not reconnect right away."
        )
        dev = await self._scan_for_saved_ring(addr, timeout=25.0)
        if self._ring_manual_disconnect or not self._ring_keep_connected:
            return

        ok = False
        if dev is not None:
            self._set_ring_connection_status("Connecting", "connecting")
            ok = await self._connect_remembered_ring(dev.address, timeout=18.0, quick=True)

        if not ok:
            self._append_ble_log(f"reconnect: saved ring advertisement not usable; trying cached address {addr}.")
            ok = await self._connect_remembered_ring(addr, timeout=12.0, quick=False)
        if not ok and self._ring_keep_connected and not self._ring_manual_disconnect:
            self._set_ring_connection_status("Reconnect failed", "error")
            self._append_ring_activity("Reconnect failed.")

    async def _scan_for_saved_ring(self, address: str, timeout: float = 25.0) -> DiscoveredDevice | None:
        wanted = address.strip().casefold()
        if not wanted:
            return None

        live_scan = getattr(self.ble, "scan_wizpr_live", None)
        if callable(live_scan):
            try:
                ordered, dev = await live_scan(
                    seconds=timeout,
                    preferred_address=address,
                    prefer_window=timeout + 1.0,
                    include_reverse_ble=True,
                )
                if ordered:
                    self._fill_ble_table(ordered)
                if dev is not None and dev.candidate_label == "WIZPR ring" and dev.address.casefold() == wanted:
                    return dev
            except Exception as scan_err:
                self._append_ble_log(
                    f"reconnect_live_scan_failed: {scan_err}; falling back to chunked discovery."
                )

        seen: dict[str, DiscoveredDevice] = {}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(1.0, float(timeout))
        while loop.time() < deadline:
            remaining = deadline - loop.time()
            scan_for = min(3.0, max(1.0, remaining))
            try:
                devs = await self.ble.scan_wizpr(seconds=scan_for, include_reverse_ble=True)
            except Exception as scan_err:
                self._append_ble_log(
                    f"reconnect_scan_failed: {scan_err}; checking Windows Bluetooth LE device list."
                )
                devs = await self.ble.watch_windows_associated_devices(scan_for)

            for candidate in devs:
                previous = seen.get(candidate.address)
                if previous is None or candidate.rssi > previous.rssi:
                    seen[candidate.address] = candidate

            ordered = sorted(seen.values(), key=BLEManager._device_sort_key)
            if ordered:
                self._fill_ble_table(ordered)

            for candidate in ordered:
                if candidate.candidate_label == "WIZPR ring" and candidate.address.casefold() == wanted:
                    return candidate

        return None

    async def _connect_remembered_ring(self, address: str, timeout: float = 18.0, quick: bool = False) -> bool:
        if not address:
            return False
        self._ring_connecting = True
        self.ring_profile.address = address
        try:
            await self.ring.connect(timeout=timeout, lookup=not quick, retry=not quick)
            await self._finish_wizpr_connection(address, load_diagnostics=False)
            return True
        except Exception as exc:
            logger.warning("Remembered WIZPR reconnect failed for %s: %s", address, exc)
            return False
        finally:
            self._ring_connecting = False

    async def _finish_wizpr_connection(self, address: str, load_diagnostics: bool = False) -> None:
        try:
            await self.ring.start_wizpr_session()
        except Exception:
            try:
                await self.ring.disconnect()
            except Exception:
                pass
            raise
        if load_diagnostics:
            self._fill_gatt_tree(await self.ring.gatt_summary())
        self._mark_ring_connected(address)

    def _connect_selected_ble(self, pair: bool = False) -> None:
        self._ensure_advanced_ble_built()
        addr = self._selected_ble_address()
        if not addr:
            self.statusBar().showMessage("No selected device; scanning for WIZPR instead.", 2500)
            self._auto_connect_wizpr()
            return
        if self._selected_ble_looks_like_case():
            self.statusBar().showMessage("That is the WIZPR charging case. Wake/remove the ring and scan again.", 6000)
            return
        label = self._selected_ble_candidate_label()
        if label and label != "WIZPR ring":
            self._append_ble_log(f"connect_note: selected device is '{label}', not confirmed WIZPR ring. Connecting for inspection.")
        self.ring_profile.address = addr
        confirmed_by_scan = label == "WIZPR ring"

        async def _run():
            self.ble_connect_btn.setEnabled(False)
            self.ble_pair_connect_btn.setEnabled(False)
            self._ring_connecting = True
            self._set_ring_connection_status("Connecting", "connecting")
            try:
                await self.ring.connect(pair=pair)
                if confirmed_by_scan or self.ring.has_wizpr_signature():
                    await self._finish_wizpr_connection(addr, load_diagnostics=True)
                    self.statusBar().showMessage(f"{'Paired and connected' if pair else 'Connected'} WIZPR: {addr}", 3000)
                else:
                    self._fill_gatt_tree(await self.ring.gatt_summary())
                    self._append_ble_log(
                        "inspection_only: connected device does not expose the WIZPR ring service "
                        "or known WIZPR characteristics; GATT tree loaded without WIZPR subscriptions."
                    )
                    self._set_ring_connection_status("Inspection", "neutral")
                    self.statusBar().showMessage(f"Connected for inspection only: {addr}", 5000)
            except Exception as e:
                logger.exception("Connect failed")
                self._set_ring_connection_status("Failed", "error")
                self.statusBar().showMessage(f"Connect failed: {e}", 5000)
            finally:
                self._ring_connecting = False
                self.ble_connect_btn.setEnabled(True)
                self.ble_pair_connect_btn.setEnabled(True)
        self.loop.create_task(_run())

    def _auto_connect_wizpr(self) -> None:
        self._cancel_saved_ring_auto_connect()
        scan_seconds = self._advanced_scan_seconds()
        dialog = QtWidgets.QProgressDialog(
            "Press the button on the ring until you see the light.\n\n"
            "Keep the ring close to this computer while Wizpr Suite scans.",
            "Cancel",
            0,
            int(scan_seconds),
            self,
        )
        dialog.setWindowTitle("Auto Connect WIZPR Ring")
        dialog.setWindowModality(QtCore.Qt.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setValue(0)
        dialog.show()
        self._set_ring_connection_status("Scanning", "scanning")
        self._append_ring_activity("Auto Connect scanning.")
        self.statusBar().showMessage(f"Auto Connect WIZPR: scanning up to {int(scan_seconds)}s.", 3000)
        QtWidgets.QApplication.processEvents()

        async def _run():
            self.wizpr_auto_btn.setEnabled(False)
            self._set_optional_button_enabled("wizpr_guided_btn", False)
            self._set_optional_button_enabled("ble_scan_btn", False)
            self._set_ring_connection_status("Scanning", "scanning")
            try:
                remembered = self._remembered_ring_address()
                if remembered:
                    dialog.setLabelText(
                        "Press the button on the ring until you see the light.\n\n"
                        "Scanning now. Saved ring will be preferred if it appears."
                    )
                    self._append_ble_log(f"auto_connect: remembered WIZPR ring saved: {remembered}.")

                seen: dict[str, DiscoveredDevice] = {}
                loop = asyncio.get_running_loop()
                deadline = loop.time() + scan_seconds
                self._append_ble_log(
                    f"auto_connect: scanning up to {scan_seconds:.0f}s. Press the ring button until the light appears."
                )

                dev: DiscoveredDevice | None = None
                ordered: list[DiscoveredDevice] = []
                live_scan = getattr(self.ble, "scan_wizpr_live", None)
                if callable(live_scan):
                    scan_task = self.loop.create_task(
                        live_scan(
                            seconds=scan_seconds,
                            preferred_address=remembered,
                            prefer_window=4.0,
                            include_reverse_ble=True,
                        )
                    )
                    try:
                        while not scan_task.done():
                            if dialog.wasCanceled():
                                scan_task.cancel()
                                try:
                                    await scan_task
                                except asyncio.CancelledError:
                                    pass
                                self._set_ring_connection_status("Canceled", "disconnected")
                                self.statusBar().showMessage("Auto Connect canceled.", 2500)
                                return

                            remaining = max(0.0, deadline - loop.time())
                            elapsed = max(0, int(scan_seconds - remaining))
                            dialog.setValue(min(int(scan_seconds), elapsed))
                            dialog.setLabelText(
                                "Press the button on the ring until you see the light.\n\n"
                                f"Scanning for WIZPR Ring... {int(remaining)}s left."
                            )
                            self.statusBar().showMessage(
                                f"Auto Connect WIZPR: {int(remaining)}s left. Press the ring button.",
                                2500,
                            )
                            await asyncio.sleep(0.25)
                        ordered, dev = await scan_task
                    except Exception as scan_err:
                        self._append_ble_log(
                            f"auto_connect_live_scan_failed: {scan_err}; checking Windows Bluetooth LE device list."
                        )
                        ordered = await self.ble.watch_windows_associated_devices(scan_seconds)
                        dev = self._choose_auto_ring_candidate(ordered)
                else:
                    chunk_seconds = 1.0
                    while True:
                        if dialog.wasCanceled():
                            self._set_ring_connection_status("Canceled", "disconnected")
                            self.statusBar().showMessage("Auto Connect canceled.", 2500)
                            return

                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            break

                        elapsed = max(0, int(scan_seconds - remaining))
                        dialog.setValue(min(int(scan_seconds), elapsed))
                        dialog.setLabelText(
                            "Press the button on the ring until you see the light.\n\n"
                            f"Scanning for WIZPR Ring... {int(remaining)}s left."
                        )
                        self.statusBar().showMessage(
                            f"Auto Connect WIZPR: {int(remaining)}s left. Press the ring button.",
                            2500,
                        )

                        scan_for = min(chunk_seconds, max(1.0, remaining))
                        try:
                            devs = await self.ble.scan_wizpr(seconds=scan_for, include_reverse_ble=True)
                        except Exception as scan_err:
                            self._append_ble_log(
                                f"auto_connect_scan_failed: {scan_err}; checking Windows Bluetooth LE device list."
                            )
                            devs = await self.ble.watch_windows_associated_devices(scan_for)

                        for candidate in devs:
                            previous = seen.get(candidate.address)
                            if previous is None or candidate.rssi > previous.rssi:
                                seen[candidate.address] = candidate

                        ordered = sorted(seen.values(), key=BLEManager._device_sort_key)
                        dev = self._choose_auto_ring_candidate(ordered)
                        if dev is not None:
                            break

                if ordered:
                    self._fill_ble_table(sorted(ordered, key=BLEManager._device_sort_key))
                if dev is None:
                    dev = self._choose_auto_ring_candidate(ordered)

                if dev is None:
                    if remembered:
                        dialog.setLabelText("No confirmed advertisement found. Trying remembered ring address...")
                        self._append_ble_log(f"auto_connect: trying remembered WIZPR ring address {remembered}.")
                        self._set_ring_connection_status("Connecting", "connecting")
                        if await self._connect_remembered_ring(remembered, timeout=18.0, quick=False):
                            dialog.setValue(int(scan_seconds))
                            dialog.setLabelText(f"Connected: {remembered}")
                            QtCore.QTimer.singleShot(1200, dialog.close)
                            self.statusBar().showMessage(f"Connected remembered ring: {remembered}", 4000)
                            return

                    dialog.setValue(int(scan_seconds))
                    self._set_ring_connection_status("Not Found", "error")
                    self._append_ring_activity("Auto Connect did not find the ring.")
                    self.statusBar().showMessage("WIZPR Ring not found during Auto Connect.", 8000)
                    self._append_ble_log(
                        "ring_not_found: No confirmed WIZPR ring advertisement appeared during Auto Connect. "
                        "Press the ring button until the light appears, keep it close to the PC, then try again."
                    )
                    return

                dialog.setLabelText("WIZPR Ring found. Connecting...")
                self._set_ring_connection_status("Connecting", "connecting")
                self._fill_ble_table([dev])
                self.ring_profile.address = dev.address
                self._ring_connecting = True
                await self.ring.connect(timeout=22.0)
                await self._finish_wizpr_connection(dev.address, load_diagnostics=self._advanced_ble_built)
                dialog.setValue(int(scan_seconds))
                dialog.setLabelText(f"Connected: {dev.name or dev.address}")
                QtCore.QTimer.singleShot(1200, dialog.close)
                self.statusBar().showMessage(f"Connected: {dev.name or dev.address}", 4000)
            except Exception as e:
                logger.exception("WIZPR auto-connect failed")
                self._set_ring_connection_status("Failed", "error")
                self._append_ring_activity(f"Auto Connect failed: {e}")
                self._append_ble_log(f"wizpr_auto_connect_failed: {e}")
                self.statusBar().showMessage(f"WIZPR auto-connect failed: {e}", 6000)
            finally:
                self._ring_connecting = False
                self.wizpr_auto_btn.setEnabled(True)
                self._set_optional_button_enabled("wizpr_guided_btn", True)
                self._set_optional_button_enabled("ble_scan_btn", True)
                if dialog.isVisible() and not dialog.wasCanceled() and self.ring_connection_status.text() != "Connected":
                    dialog.close()
        self.loop.create_task(_run())

    def _guided_ring_search(self) -> None:
        self._ensure_advanced_ble_built()

        async def _run():
            self.wizpr_auto_btn.setEnabled(False)
            self.wizpr_guided_btn.setEnabled(False)
            self.ble_scan_btn.setEnabled(False)
            seen: dict[str, DiscoveredDevice] = {}
            chunk_seconds = 5.0
            loops = 12

            self._append_ble_log(
                "guided_search: Running SDK-style WIZPR discovery for 60s."
            )
            try:
                for idx in range(loops):
                    remaining = int((loops - idx) * chunk_seconds)
                    self._set_ring_connection_status("Scanning", "scanning")
                    self.statusBar().showMessage(f"Guided WIZPR search: {remaining}s left. Press the ring button now.", 2500)
                    try:
                        devs = await self.ble.scan_wizpr(seconds=chunk_seconds, include_reverse_ble=True)
                    except Exception as scan_err:
                        self._append_ble_log(
                            f"guided_search_scan_failed: {scan_err}; checking Windows Bluetooth LE device list for this chunk."
                        )
                        devs = await self.ble.watch_windows_associated_devices(chunk_seconds)

                    for dev in devs:
                        previous = seen.get(dev.address)
                        if previous is None or dev.rssi > previous.rssi:
                            seen[dev.address] = dev
                        if previous is None:
                            self._append_ble_log(
                                f"guided_search_new: {dev.candidate_label}: {dev.name or '(no name)'} "
                                f"[{dev.address}] RSSI={dev.rssi}"
                            )

                    ordered = sorted(
                        seen.values(),
                        key=BLEManager._device_sort_key,
                    )
                    self._fill_ble_table(ordered)

                    ring = self._choose_auto_ring_candidate(ordered)
                    if ring is not None:
                        self._append_ble_log(
                            f"guided_search_found: WIZPR ring candidate {ring.name or '(no name)'} [{ring.address}]. Connecting..."
                        )
                        self.ring_profile.address = ring.address
                        self._set_ring_connection_status("Connecting", "connecting")
                        self._ring_connecting = True
                        await self.ring.connect(timeout=22.0)
                        await self._finish_wizpr_connection(ring.address, load_diagnostics=True)
                        self.statusBar().showMessage(f"Connected WIZPR: {ring.name or ring.address}", 5000)
                        return

                possible = [d for d in seen.values() if d.candidate_label == "Possible WIZPR/Silicon Labs"]
                if possible:
                    self._append_ble_log(
                        "guided_search_done: No confirmed WIZPR ring service/name appeared, but a Silicon Labs-looking device was seen. "
                        "Select it and use Connect for inspection if it appeared exactly when you pressed the ring."
                    )
                else:
                    self._append_ble_log(
                        "guided_search_done: No WIZPR ring advertisement appeared during SDK-style discovery. "
                        "Run ble_watch for a raw all-device capture if another advertisement changes when the ring is used."
                    )
                self._set_ring_connection_status("Not Found", "error")
                self.statusBar().showMessage("Guided search finished without finding a WIZPR ring.", 8000)
            except Exception as e:
                logger.exception("Guided WIZPR search failed")
                self._set_ring_connection_status("Failed", "error")
                self._append_ble_log(f"guided_search_failed: {e}")
                self.statusBar().showMessage(f"Guided search failed: {e}", 6000)
            finally:
                self._ring_connecting = False
                self.wizpr_auto_btn.setEnabled(True)
                self.wizpr_guided_btn.setEnabled(True)
                self.ble_scan_btn.setEnabled(True)
        self.loop.create_task(_run())

    def _subscribe_wizpr_channels(self) -> None:
        async def _run():
            try:
                await self.ring.subscribe_wizpr_channels(strict=False, include_fallback=True)
                self.statusBar().showMessage("WIZPR channels subscribed.", 2500)
            except Exception as e:
                logger.exception("WIZPR subscribe failed")
                self._append_ble_log(
                    "wizpr_subscribe_failed: connect to a device labeled WIZPR ring, or one exposing "
                    "the WIZPR service 00000000-dc2e-4362-93d3-df429eb3ad10."
                )
                self.statusBar().showMessage(f"WIZPR subscribe failed: {e}", 5000)
        self.loop.create_task(_run())

    def _query_wizpr_battery(self) -> None:
        async def _run():
            try:
                await self.ring.query_battery()
                self.statusBar().showMessage("Battery query sent.", 2000)
            except Exception as e:
                self.statusBar().showMessage(f"Battery query failed: {e}", 4000)
        self.loop.create_task(_run())

    def _query_wizpr_proxy(self) -> None:
        async def _run():
            try:
                await self.ring.query_proxy()
                self.statusBar().showMessage("Proximity check sent.", 2000)
            except Exception as e:
                self.statusBar().showMessage(f"Proximity check failed: {e}", 4000)
        self.loop.create_task(_run())

    def _query_wizpr_version(self) -> None:
        async def _run():
            try:
                await self.ring.query_version()
                self.statusBar().showMessage("Version query sent.", 2000)
            except Exception as e:
                self.statusBar().showMessage(f"Version query failed: {e}", 4000)
        self.loop.create_task(_run())

    def _lock_wizpr(self) -> None:
        async def _run():
            try:
                await self.ring.lock()
                self.statusBar().showMessage("LOCK sent.", 2000)
            except Exception as e:
                self.statusBar().showMessage(f"LOCK failed: {e}", 4000)
        self.loop.create_task(_run())

    def _sleep_wizpr(self) -> None:
        async def _run():
            try:
                await self.ring.sleep()
                self.statusBar().showMessage("SLEEP sent.", 2000)
            except Exception as e:
                self.statusBar().showMessage(f"SLEEP failed: {e}", 4000)
        self.loop.create_task(_run())

    def _disconnect_ble(self) -> None:
        async def _run():
            self._ring_manual_disconnect = True
            self._ring_keep_connected = False
            self._cancel_ring_background_tasks()
            try:
                await self.ring.disconnect()
                self._set_ring_connection_status("Disconnected", "disconnected")
                self.ring_connection_status.setToolTip("")
                self._append_ring_activity("Disconnected.")
                self.statusBar().showMessage("Disconnected", 2000)
            except Exception:
                self._set_ring_connection_status("Disconnected", "disconnected")
                self.ring_connection_status.setToolTip("")
                self._append_ring_activity("Disconnected.")
                self.statusBar().showMessage("Disconnected (forced)", 2000)
        self.loop.create_task(_run())

    def _refresh_gatt(self) -> None:
        self._ensure_advanced_ble_built()

        async def _run():
            try:
                self.gatt_tree.clear()
                summary = await self.ring.gatt_summary()
                self._fill_gatt_tree(summary)
                self.statusBar().showMessage("GATT refreshed.", 2000)
            except Exception as e:
                logger.exception("GATT refresh failed")
                self.statusBar().showMessage(f"GATT refresh failed: {e}", 4000)
        self.loop.create_task(_run())

    def _fill_gatt_tree(self, summary: list[dict[str, Any]]) -> None:
        tree = getattr(self, "gatt_tree", None)
        if tree is None:
            return
        self.gatt_tree.clear()
        for s in summary:
            s_item = QtWidgets.QTreeWidgetItem([f"{s['uuid']}  {s.get('description','')}".strip(), ""])
            self.gatt_tree.addTopLevelItem(s_item)
            for c in s.get("characteristics", []):
                props = ", ".join(c.get("properties", []))
                c_item = QtWidgets.QTreeWidgetItem([f"{c['uuid']}  {c.get('description','')}".strip(), props])
                c_item.setData(0, QtCore.Qt.UserRole, c.get("uuid"))
                s_item.addChild(c_item)
        self.gatt_tree.expandAll()

    def _selected_char_uuid(self) -> str:
        tree = getattr(self, "gatt_tree", None)
        if tree is None:
            return ""
        item = self.gatt_tree.currentItem()
        if not item:
            return ""
        u = item.data(0, QtCore.Qt.UserRole)
        return str(u) if u else ""

    def _subscribe_selected_char(self) -> None:
        self._ensure_advanced_ble_built()
        uuid = self._selected_char_uuid()
        if not uuid:
            self.statusBar().showMessage("Select a characteristic row.", 2000)
            return

        async def _run():
            try:
                await self.ring.subscribe(uuid)
                self.statusBar().showMessage(f"Subscribed: {uuid}", 2500)
            except Exception as e:
                logger.exception("Subscribe failed")
                self.statusBar().showMessage(f"Subscribe failed: {e}", 4000)
        self.loop.create_task(_run())

    def _unsubscribe_selected_char(self) -> None:
        self._ensure_advanced_ble_built()
        uuid = self._selected_char_uuid()
        if not uuid:
            return

        async def _run():
            try:
                await self.ring.unsubscribe(uuid)
                self.statusBar().showMessage(f"Unsubscribed: {uuid}", 2000)
            except Exception:
                self.statusBar().showMessage("Unsubscribed (forced)", 2000)
        self.loop.create_task(_run())


    def _active_llm_detail_text(self, suffix: str = "") -> str:
        pid = self.active_llm_id
        if pid == "ollama":
            model = (self.cfg.ollama.model or "").strip() or "select a model"
            base = f"Ollama: {model}"
        elif pid == "openai_compat":
            model = (self.cfg.openai_compat.model or "").strip() or "select a model"
            base = f"Compatible: {model}"
        else:
            model = (self.cfg.openai.model or "").strip() or "select a model"
            base = f"OpenAI: {model}"
            if not (self.cfg.openai.api_key or os.environ.get("OPENAI_API_KEY", "").strip()):
                suffix = suffix or "key needed"
        return f"{base} ({suffix})" if suffix else base

    def _refresh_active_llm_detail(self, suffix: str = "") -> None:
        detail = self._active_llm_detail_text(suffix)
        label = getattr(self, "active_llm_detail", None)
        if label is not None:
            label.setText(detail)
        settings_summary = getattr(self, "settings_provider_summary", None)
        if settings_summary is not None:
            settings_summary.setText(detail)

    def _choose_ollama_model(self, models: list[str], current: str = "") -> str:
        current = (current or "").strip()
        if current and current in models:
            return current
        ordered = sort_ollama_models(models)
        return ordered[0] if ordered else current

    def _schedule_active_llm_status_check(self) -> None:
        self._refresh_active_llm_detail()
        if self.active_llm_id != "ollama":
            return

        task = self._active_llm_status_task
        if task is not None and not task.done():
            task.cancel()
        self._active_llm_status_task = self.loop.create_task(self._refresh_active_llm_status())

    async def _refresh_active_llm_status(self) -> None:
        if self.active_llm_id != "ollama":
            return
        self._refresh_active_llm_detail("checking")
        try:
            url, msg = await asyncio.wait_for(
                self.p_ollama.discover_base_url(
                    self.cfg.ollama.base_url,
                    timeout=OLLAMA_STATUS_DISCOVERY_TIMEOUT_SECONDS,
                ),
                timeout=OLLAMA_STATUS_DISCOVERY_TIMEOUT_SECONDS + 0.5,
            )
        except asyncio.TimeoutError:
            self._refresh_active_llm_detail("checking timed out")
            logger.info("Ollama auto-discovery timed out during status check.")
            return
        if not url:
            self._refresh_active_llm_detail("not found")
            logger.info("Ollama auto-discovery failed: %s", msg)
            return

        self._apply_discovered_ollama_url()
        try:
            models, err = await asyncio.wait_for(
                self.p_ollama.list_models(),
                timeout=OLLAMA_STATUS_MODEL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            self._refresh_active_llm_detail(f"found at {url}")
            logger.info("Ollama model list timed out after discovery.")
            return
        if err:
            self._refresh_active_llm_detail(f"found at {url}")
            logger.info("Ollama model list failed after discovery: %s", err)
            return

        chosen = self._choose_ollama_model(models, self.cfg.ollama.model)
        if chosen and chosen != self.cfg.ollama.model:
            self.cfg.ollama.model = chosen
            if hasattr(self, "ollama_model"):
                self._set_combo_models(self.ollama_model, models, chosen)
            save_config(self.app_dir, self.cfg)
        elif hasattr(self, "ollama_model"):
            self._set_combo_models(self.ollama_model, models, chosen)
        self._refresh_active_llm_detail(f"{len(models)} model(s)")
        if chosen and callable(getattr(self.p_ollama, "warm_model", None)):
            task = self._ollama_warm_task
            if task is not None and not task.done():
                task.cancel()
            self._ollama_warm_task = self.loop.create_task(self._warm_ollama_model(chosen))

    async def _warm_ollama_model(self, model: str) -> None:
        try:
            error = await self.p_ollama.warm_model(model)
            if error:
                logger.info("Ollama model warmup failed: %s", error)
        except asyncio.CancelledError:
            return
        finally:
            self._ollama_warm_task = None

    def _on_active_llm_changed(self, pid: str) -> None:
        if pid and pid in self.registry.list_ids():
            self.active_llm_id = pid
            self.cfg.active_llm_id = pid
            save_config(self.app_dir, self.cfg)
            self._set_active_llm_combo(pid)
            self._schedule_active_llm_status_check()
            self.statusBar().showMessage(f"Active LLM: {self._provider_label(pid)}.", 1500)

    def _save_openai(self) -> None:
        old_key = self.cfg.openai.api_key
        old_base = self.cfg.openai.base_url
        old_model = self.cfg.openai.model
        old_transcription_model = self.cfg.openai.transcription_model
        if hasattr(self, "openai_key"):
            self.cfg.openai.api_key = self.openai_key.text().strip()
        if hasattr(self, "openai_base"):
            self.cfg.openai.base_url = self.openai_base.text().strip()
        if hasattr(self, "openai_model"):
            self.cfg.openai.model = self.openai_model.currentText().strip()
        if hasattr(self, "openai_transcription_model"):
            self.cfg.openai.transcription_model = self.openai_transcription_model.text().strip() or "gpt-4o-transcribe"
        connection_changed = old_key != self.cfg.openai.api_key or old_base != self.cfg.openai.base_url
        changed = connection_changed or old_model != self.cfg.openai.model or old_transcription_model != self.cfg.openai.transcription_model
        if connection_changed:
            self.p_openai.configure(self.cfg.openai.api_key, self.cfg.openai.base_url)
        if changed:
            save_config(self.app_dir, self.cfg)
        self.statusBar().showMessage("OpenAI saved.", 1500)

    def _save_ollama(self) -> None:
        old_base = self.cfg.ollama.base_url
        old_model = self.cfg.ollama.model
        base_url = self.ollama_url.text().strip() if hasattr(self, "ollama_url") else self.cfg.ollama.base_url
        self.p_ollama.configure(base_url)
        self.cfg.ollama.base_url = self.p_ollama.base_url
        if hasattr(self, "ollama_url"):
            self.ollama_url.setText(self.cfg.ollama.base_url)
        if hasattr(self, "ollama_model"):
            self.cfg.ollama.model = self.ollama_model.currentText().strip()
        if old_base != self.cfg.ollama.base_url or old_model != self.cfg.ollama.model:
            save_config(self.app_dir, self.cfg)
        self._refresh_active_llm_detail()
        self.statusBar().showMessage("Ollama saved.", 1500)

    def _save_compat(self) -> None:
        old_base = self.cfg.openai_compat.base_url
        old_key = self.cfg.openai_compat.api_key
        old_model = self.cfg.openai_compat.model
        if hasattr(self, "compat_url"):
            self.cfg.openai_compat.base_url = self.compat_url.text().strip()
        if hasattr(self, "compat_key"):
            self.cfg.openai_compat.api_key = self.compat_key.text().strip()
        if hasattr(self, "compat_model"):
            self.cfg.openai_compat.model = self.compat_model.currentText().strip()
        connection_changed = old_base != self.cfg.openai_compat.base_url or old_key != self.cfg.openai_compat.api_key
        changed = connection_changed or old_model != self.cfg.openai_compat.model
        if connection_changed:
            self.p_compat.configure(self.cfg.openai_compat.base_url, self.cfg.openai_compat.api_key)
        if changed:
            save_config(self.app_dir, self.cfg)
        self.statusBar().showMessage("Compat saved.", 1500)

    def _save_transcription(self, show_status: bool = False) -> None:
        if hasattr(self, "transcription_backend"):
            self.cfg.transcription.stt_backend = str(self.transcription_backend.currentData() or "auto")
        if hasattr(self, "openai_transcription_model"):
            self.cfg.openai.transcription_model = self.openai_transcription_model.text().strip() or "gpt-4o-transcribe"
        if hasattr(self, "local_transcription_model"):
            self.cfg.transcription.local_model = self.local_transcription_model.currentText().strip() or "small.en"
        if hasattr(self, "local_transcription_compute"):
            self.cfg.transcription.local_compute_type = self.local_transcription_compute.currentText().strip() or "int8"
        if hasattr(self, "transcription_warm_start"):
            self.cfg.transcription.warm_at_startup = bool(self.transcription_warm_start.isChecked())
        if hasattr(self, "transcription_warm_after_connect"):
            self.cfg.transcription.warm_after_connect = bool(self.transcription_warm_after_connect.isChecked())
        if hasattr(self, "transcription_require_wake"):
            self.cfg.transcription.require_wake_word = bool(self.transcription_require_wake.isChecked())
        elif hasattr(self, "wake_required_check") and not self._target_always_requires_wake_phrase(self._ring_voice_target_value()):
            self.cfg.transcription.require_wake_word = bool(self.wake_required_check.isChecked())
        if hasattr(self, "transcription_hold_coding"):
            self.cfg.transcription.hold_coding_voice_commands = bool(self.transcription_hold_coding.isChecked())
        if hasattr(self, "transcription_assistant_wake"):
            self.cfg.transcription.assistant_wake_word = self.transcription_assistant_wake.text().strip() or "Wizpr, Assistant"
        if hasattr(self, "transcription_codex_wake"):
            self.cfg.transcription.codex_wake_word = self.transcription_codex_wake.text().strip() or "Codex"
        if hasattr(self, "transcription_opencode_wake"):
            self.cfg.transcription.opencode_wake_word = self.transcription_opencode_wake.text().strip() or "OpenCode, Open Code"
        if hasattr(self, "transcription_clipboard_wake"):
            self.cfg.transcription.clipboard_wake_word = self.transcription_clipboard_wake.text().strip() or "Wizpr"
        if hasattr(self, "transcription_paste_wake"):
            self.cfg.transcription.paste_wake_word = self.transcription_paste_wake.text().strip() or "Wizpr"
        if not hasattr(self, "transcription_assistant_wake"):
            self._apply_simple_wake_phrase_to_config()
        if hasattr(self, "transcription_finalize_delay"):
            self.cfg.transcription.ring_audio_finalize_delay_ms = int(self.transcription_finalize_delay.value())
            self.ring.audio_finalize_delay = self.cfg.transcription.ring_audio_finalize_delay_ms / 1000.0
        if hasattr(self, "transcription_voice_mode"):
            self.cfg.transcription.voice_mode = str(self.transcription_voice_mode.currentData() or "proximity")
        elif hasattr(self, "ring_voice_mode"):
            self.cfg.transcription.voice_mode = str(self.ring_voice_mode.currentData() or "proximity")
        if hasattr(self, "transcription_sleep_timeout"):
            self.cfg.transcription.ring_sleep_timeout_seconds = int(self.transcription_sleep_timeout.currentData() or 0)
        elif hasattr(self, "ring_sleep_timeout"):
            self.cfg.transcription.ring_sleep_timeout_seconds = int(self.ring_sleep_timeout.currentData() or 0)
        if hasattr(self, "audio_preflight_enabled"):
            self.cfg.transcription.audio_preflight_enabled = bool(self.audio_preflight_enabled.isChecked())
        if hasattr(self, "audio_preflight_min_seconds"):
            self.cfg.transcription.audio_preflight_min_seconds = float(self.audio_preflight_min_seconds.value())
        if hasattr(self, "audio_preflight_min_active"):
            self.cfg.transcription.audio_preflight_min_active_seconds = float(self.audio_preflight_min_active.value())
        if hasattr(self, "ring_connection_sound"):
            self.cfg.transcription.ring_connection_sound = bool(self.ring_connection_sound.isChecked())
        elif hasattr(self, "ring_connect_sound_check"):
            self.cfg.transcription.ring_connection_sound = bool(self.ring_connect_sound_check.isChecked())
        if hasattr(self, "mic_activation_sound"):
            self.cfg.transcription.mic_activation_sound = bool(self.mic_activation_sound.isChecked())
        elif hasattr(self, "ring_mic_sound_check"):
            self.cfg.transcription.mic_activation_sound = bool(self.ring_mic_sound_check.isChecked())
        if hasattr(self, "low_battery_warning"):
            self.cfg.transcription.low_battery_warning = bool(self.low_battery_warning.isChecked())
        elif hasattr(self, "ring_low_battery_check"):
            self.cfg.transcription.low_battery_warning = bool(self.ring_low_battery_check.isChecked())
        if hasattr(self, "protect_ring_buttons_check"):
            self.cfg.protect_connected_ring_buttons = bool(self.protect_ring_buttons_check.isChecked())
        if hasattr(self, "tts_voice"):
            self.cfg.transcription.tts_voice = self.tts_voice.text().strip()
        if hasattr(self, "tts_rate"):
            self.cfg.transcription.tts_rate = int(self.tts_rate.value())
        if hasattr(self, "speak_responses_check"):
            self.cfg.transcription.speak_responses = bool(self.speak_responses_check.isChecked())
        elif hasattr(self, "ring_tts_response_check"):
            self.cfg.transcription.speak_responses = bool(self.ring_tts_response_check.isChecked())
        if hasattr(self, "interrupt_mode_combo"):
            mode = str(self.interrupt_mode_combo.currentData() or "word")
            self.cfg.transcription.interrupt_mode = mode if mode in {"ring", "word", "both", "off"} else "word"
        if hasattr(self, "interrupt_word_edit"):
            self.cfg.transcription.interrupt_word = self.interrupt_word_edit.text().strip() or "stop"
        save_config(self.app_dir, self.cfg)
        self._sync_wake_required_ui()
        self._sync_wake_phrase_ui()
        self._sync_ring_settings_ui()
        self._refresh_quick_ready()
        if show_status:
            self.statusBar().showMessage("Voice settings saved.", 1500)

    def _warm_transcriber_now(self) -> None:
        async def _run() -> None:
            self._save_transcription()
            self.transcription_warm.setEnabled(False)
            model = self.cfg.transcription.local_model or "small.en"
            compute = self.cfg.transcription.local_compute_type or "int8"
            self.statusBar().showMessage(f"Warming local transcription model: {model}...", 4000)
            try:
                err = await warm_local_transcriber(model_name=model, compute_type=compute)
                if err:
                    self.statusBar().showMessage(f"Transcription warmup failed: {err}", 8000)
                else:
                    self.statusBar().showMessage(f"Local transcription model ready: {model}", 4000)
            finally:
                self.transcription_warm.setEnabled(True)

        self.loop.create_task(_run())

    def _detect_codex(self) -> None:
        path = detect_codex_executable()
        if path:
            self.codex_exe.setText(path)
            self.statusBar().showMessage("Codex CLI detected.", 2000)
        else:
            self.statusBar().showMessage("Codex CLI not found.", 3000)

    def _save_codex(self) -> None:
        if hasattr(self, "codex_exe"):
            self.cfg.codex.executable = self.codex_exe.text().strip()
        if hasattr(self, "codex_model"):
            self.cfg.codex.model = self.codex_model.text().strip()
        if hasattr(self, "codex_workspace"):
            self.cfg.codex.working_dir = self.codex_workspace.text().strip()
        if hasattr(self, "codex_sandbox"):
            self.cfg.codex.sandbox = self.codex_sandbox.currentText().strip()
        if hasattr(self, "codex_timeout"):
            self.cfg.codex.timeout_seconds = float(self.codex_timeout.value())
        save_config(self.app_dir, self.cfg)
        self.statusBar().showMessage("Codex saved.", 1500)

    def _detect_opencode(self) -> None:
        path = detect_opencode_executable()
        if path:
            self.opencode_exe.setText(path)
            self.statusBar().showMessage("OpenCode CLI detected.", 2000)
        else:
            self.statusBar().showMessage("OpenCode CLI not found.", 3000)

    def _save_opencode(self) -> None:
        if hasattr(self, "opencode_exe"):
            self.cfg.opencode.executable = self.opencode_exe.text().strip()
        if hasattr(self, "opencode_model"):
            self.cfg.opencode.model = self.opencode_model.currentText().strip() or "ollama/qwen36-27b"
        if hasattr(self, "opencode_workspace"):
            self.cfg.opencode.working_dir = self.opencode_workspace.text().strip()
        if hasattr(self, "opencode_timeout"):
            self.cfg.opencode.timeout_seconds = float(self.opencode_timeout.value())
        if hasattr(self, "opencode_continue"):
            self.cfg.opencode.continue_session = bool(self.opencode_continue.isChecked())
        if hasattr(self, "opencode_auto"):
            self.cfg.opencode.auto_approve = bool(self.opencode_auto.isChecked())
        save_config(self.app_dir, self.cfg)
        self.statusBar().showMessage("OpenCode saved.", 1500)

    def _health_check(self, pid: str) -> None:
        async def _run():
            p = self.registry.get(pid)
            if not p:
                return
            if pid == "ollama":
                self.p_ollama.configure(self.ollama_url.text().strip())
            ok, msg = await p.is_healthy()
            if pid == "ollama" and ok:
                self._apply_discovered_ollama_url()
            self.statusBar().showMessage(f"{pid} healthy" if ok else f"{pid} unhealthy: {msg}", 4000)
        self.loop.create_task(_run())

    def _refresh_openai_models(self) -> None:
        async def _run():
            self.openai_fetch.setEnabled(False)
            try:
                key = self.openai_key.text().strip() or os.environ.get("OPENAI_API_KEY","").strip()
                if not key:
                    self.statusBar().showMessage("OpenAI key required to fetch models.", 3000)
                    return
                self.p_openai.configure(key, self.openai_base.text().strip())
                models, err = await self.p_openai.list_models()
                if err:
                    self.statusBar().showMessage(f"OpenAI models failed: {err}", 5000)
                    return
                keep = self.openai_model.currentText().strip()
                self._set_combo_models(self.openai_model, models, keep)
                self.statusBar().showMessage(f"Loaded {len(models)} models.", 2500)
            finally:
                self.openai_fetch.setEnabled(True)
        self.loop.create_task(_run())

    def _refresh_ollama_models(self) -> None:
        async def _run():
            self.ollama_fetch.setEnabled(False)
            try:
                self.p_ollama.configure(self.ollama_url.text().strip())
                models, err = await self.p_ollama.list_models()
                self._apply_discovered_ollama_url()
                if err:
                    self.statusBar().showMessage(f"Ollama models failed: {err}", 5000)
                    return
                keep = self._choose_ollama_model(models, self.ollama_model.currentText().strip())
                self._set_combo_models(self.ollama_model, models, keep)
                self.cfg.ollama.model = keep
                save_config(self.app_dir, self.cfg)
                self._refresh_active_llm_detail(f"{len(models)} model(s)")
                self.statusBar().showMessage(f"Loaded {len(models)} models.", 2500)
            finally:
                self.ollama_fetch.setEnabled(True)
        self.loop.create_task(_run())

    def _find_ollama_server(self) -> None:
        async def _run():
            self.ollama_find.setEnabled(False)
            self.ollama_fetch.setEnabled(False)
            self.ollama_health.setEnabled(False)
            try:
                preferred = self.ollama_url.text().strip()
                url, msg = await self.p_ollama.discover_base_url(preferred)
                if not url:
                    self.statusBar().showMessage(f"Ollama not found: {msg}", 7000)
                    return
                self._apply_discovered_ollama_url()
                models, err = await self.p_ollama.list_models()
                if not err:
                    keep = self._choose_ollama_model(models, self.ollama_model.currentText().strip())
                    self._set_combo_models(self.ollama_model, models, keep)
                    self.cfg.ollama.model = keep
                    save_config(self.app_dir, self.cfg)
                    self._refresh_active_llm_detail(f"{len(models)} model(s)")
                    self.statusBar().showMessage(f"Ollama found at {url}; loaded {len(models)} model(s).", 5000)
                else:
                    self._refresh_active_llm_detail(f"found at {url}")
                    self.statusBar().showMessage(f"Ollama found at {url}; model fetch failed: {err}", 7000)
            finally:
                self.ollama_find.setEnabled(True)
                self.ollama_fetch.setEnabled(True)
                self.ollama_health.setEnabled(True)
        self.loop.create_task(_run())

    def _apply_discovered_ollama_url(self) -> None:
        self.cfg.ollama.base_url = self.p_ollama.base_url
        if hasattr(self, "ollama_url"):
            self.ollama_url.setText(self.cfg.ollama.base_url)
        save_config(self.app_dir, self.cfg)

    def _refresh_compat_models(self) -> None:
        async def _run():
            self.compat_fetch.setEnabled(False)
            try:
                self.p_compat.configure(self.compat_url.text().strip(), self.compat_key.text().strip())
                models, err = await self.p_compat.list_models()
                if err:
                    self.statusBar().showMessage(f"Compat models: {err}", 5000)
                    return
                keep = self.compat_model.currentText().strip()
                self._set_combo_models(self.compat_model, models, keep)
                self.statusBar().showMessage(f"Loaded {len(models)} models.", 2500)
            finally:
                self.compat_fetch.setEnabled(True)
        self.loop.create_task(_run())

    def _refresh_opencode_models(self) -> None:
        async def _run():
            self.opencode_fetch.setEnabled(False)
            try:
                models, err = await list_opencode_models(self.opencode_exe.text().strip())
                if err:
                    self.statusBar().showMessage(f"OpenCode models failed: {err}", 6000)
                    return
                keep = self.opencode_model.currentText().strip()
                self._set_combo_models(self.opencode_model, models, keep or "ollama/qwen36-27b")
                self.statusBar().showMessage(f"Loaded {len(models)} OpenCode model(s).", 2500)
            finally:
                self.opencode_fetch.setEnabled(True)
        self.loop.create_task(_run())

    def _test_opencode(self) -> None:
        async def _run() -> None:
            self.opencode_test.setEnabled(False)
            try:
                await self._send_prompt_to_opencode("Reply with READY if the OpenCode bridge is working.")
            finally:
                self.opencode_test.setEnabled(True)
        self.loop.create_task(_run())

    def _use_codex_for_ring_voice(self) -> None:
        self._set_ring_voice_target("codex")

    def _use_opencode_for_ring_voice(self) -> None:
        self._set_ring_voice_target("opencode")

    def _set_combo_models(self, combo: QtWidgets.QComboBox, models: list[str], keep: str) -> None:
        combo.blockSignals(True)
        try:
            combo.clear()
            for m in models:
                combo.addItem(m)
            if keep and keep not in models:
                combo.insertItem(0, keep)
            combo.setCurrentText(keep)
        finally:
            combo.blockSignals(False)

    def _memory_enabled_changed(self, checked: bool) -> None:
        self.cfg.memory.enabled = bool(checked)
        save_config(self.app_dir, self.cfg)
        state = "enabled" if checked else "disabled"
        self.statusBar().showMessage(f"Persistent memory {state}.", 1800)

    def _show_memory_dialog(self) -> None:
        stats = self.memory.stats()
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Wizpr Suite Memory")
        box.setIcon(QtWidgets.QMessageBox.Information)
        box.setText(f"Saved facts: {stats.facts}\nSaved conversation turns: {stats.turns}")
        box.setInformativeText(
            "Memory is stored locally in your Wizpr Suite app-data folder. "
            "Say or type 'remember that ...' to save a permanent fact."
        )
        clear_history = box.addButton("Clear Conversation", QtWidgets.QMessageBox.DestructiveRole)
        clear_all = box.addButton("Clear All Memory", QtWidgets.QMessageBox.DestructiveRole)
        box.addButton(QtWidgets.QMessageBox.Close)
        box.exec()
        clicked = box.clickedButton()
        if clicked is clear_history:
            self.memory.clear_history()
            self.statusBar().showMessage("Conversation memory cleared.", 2200)
        elif clicked is clear_all:
            self.memory.clear_all()
            self.statusBar().showMessage("All persistent memory cleared.", 2200)

    def _tool_permission_changed(self) -> None:
        combo = getattr(self, "tool_permission_combo", None)
        if combo is None:
            return
        mode = str(combo.currentData() or "ask")
        self.cfg.tools.permission_mode = mode if mode in {"off", "ask", "allow"} else "ask"
        if self.cfg.tools.permission_mode == "off":
            self._pending_tool_request = None
            if hasattr(self, "run_tool_btn"):
                self.run_tool_btn.setEnabled(False)
        save_config(self.app_dir, self.cfg)
        labels = {"off": "disabled", "ask": "approval required", "allow": "auto-allow enabled"}
        self.statusBar().showMessage(f"Desktop tools: {labels[self.cfg.tools.permission_mode]}.", 2200)

    def _interrupt_settings_changed(self) -> None:
        combo = getattr(self, "interrupt_mode_combo", None)
        edit = getattr(self, "interrupt_word_edit", None)
        if combo is not None:
            mode = str(combo.currentData() or "word")
            self.cfg.transcription.interrupt_mode = mode if mode in {"ring", "word", "both", "off"} else "word"
        if edit is not None:
            self.cfg.transcription.interrupt_word = edit.text().strip() or "stop"
        save_config(self.app_dir, self.cfg)
        self.statusBar().showMessage("Interrupt settings saved.", 1600)

    def _assistant_response_active(self) -> bool:
        if int(getattr(self, "_active_response_count", 0) or 0) > 0:
            return True
        task = getattr(self, "_speech_task", None)
        return bool((task is not None and not task.done()) or getattr(self, "_speech_queue", []))

    def _interrupt_mode(self) -> str:
        cfg = getattr(self, "cfg", None)
        transcription = getattr(cfg, "transcription", None)
        mode = str(getattr(transcription, "interrupt_mode", "ring") or "ring")
        return mode if mode in {"ring", "word", "both", "off"} else "ring"

    def _interrupt_phrases(self) -> list[str]:
        raw = self.cfg.transcription.interrupt_word or "stop"
        return [" ".join(item.strip().split()) for item in raw.split(",") if item.strip()]

    def _interrupt_command_from_transcript(self, transcript: str) -> tuple[bool, str]:
        text = " ".join((transcript or "").strip().split())
        lowered = text.casefold()
        for phrase in sorted(self._interrupt_phrases(), key=len, reverse=True):
            phrase_lower = phrase.casefold()
            match = re.search(rf"(?<!\w){re.escape(phrase_lower)}(?!\w)", lowered)
            if match is None:
                continue
            before = text[:match.start()].rstrip(" ,;:-")
            after = text[match.end():].lstrip(" ,.!?;:-")
            remainder = f"{before} {after}".strip()
            return True, " ".join(remainder.split())
        return False, text

    def _commit_interrupt(self) -> int:
        self._voice_turn += 1
        self._response_generation += 1
        self._stop_speech(show_status=False)
        self._cancel_voice_pipeline()
        self._cancel_assistant_responses()
        self._set_voice_status("Voice: interrupted")
        return self._voice_turn

    async def _execute_tool_request(self, request: DesktopToolRequest) -> bool:
        ok, message = await execute_desktop_tool(request)
        if ok:
            self.output.appendPlainText(f"[tool] {message}\n")
            self._maybe_speak_response(message)
            self.statusBar().showMessage(message, 2500)
        else:
            self.output.appendPlainText(f"[tool error] {message}\n")
            self.statusBar().showMessage(f"Tool failed: {message}", 5000)
        return ok

    def _hold_tool_request(self, request: DesktopToolRequest) -> None:
        self._pending_tool_request = request
        if hasattr(self, "prompt"):
            self.prompt.setPlainText(request.original)
        if hasattr(self, "run_tool_btn"):
            self.run_tool_btn.setEnabled(True)
        self.output.appendPlainText(
            f"[tool approval] {request.label} is ready. Click Run Tool to approve.\n"
        )
        self.statusBar().showMessage(f"Approval required to open {request.label}.", 6000)

    def _run_pending_tool(self) -> None:
        request = self._pending_tool_request
        self._pending_tool_request = None
        if hasattr(self, "run_tool_btn"):
            self.run_tool_btn.setEnabled(False)
        if request is None:
            self.statusBar().showMessage("No desktop tool is waiting for approval.", 2000)
            return
        self.loop.create_task(self._execute_tool_request(request))

    async def _handle_desktop_tool(self, prompt: str) -> bool:
        request = parse_desktop_tool_request(prompt)
        if request is None:
            return False
        mode = self.cfg.tools.permission_mode
        if mode == "off":
            message = "Desktop tools are disabled in Wizpr Suite settings."
            self.output.appendPlainText(f"[tool disabled] {message}\n")
            self._maybe_speak_response(message)
            self.statusBar().showMessage(message, 4000)
            return True
        if mode == "ask":
            self._hold_tool_request(request)
            return True
        await self._execute_tool_request(request)
        return True

    def _memory_prompt(self, prompt: str) -> str:
        if not self.cfg.memory.enabled:
            return prompt
        context = self.memory.context(
            max_recent_turns=self.cfg.memory.max_recent_turns,
            max_characters=self.cfg.memory.max_context_characters,
        )
        if not context:
            return prompt
        return f"{context}\n\nCurrent user request:\n{prompt}"

    def _apply_explicit_memory_command(self, prompt: str) -> None:
        if not self.cfg.memory.enabled:
            return
        result = self.memory.apply_explicit_memory_command(prompt)
        if result is None:
            return
        action, value = result
        if action == "remember":
            message = "Saved to memory." if value else "That was already in memory."
        else:
            removed = int(value or 0)
            message = f"Removed {removed} matching memory item{'s' if removed != 1 else ''}."
        self.output.appendPlainText(f"[memory] {message}\n")

    def _speak_responses_changed(self, checked: bool) -> None:
        self.cfg.transcription.speak_responses = bool(checked)
        if not checked:
            self._stop_speech(show_status=False)
        self._save_transcription()
        self.statusBar().showMessage("Spoken responses on." if checked else "Spoken responses off.", 1500)

    def _stop_speech(self, checked: bool = False, show_status: bool = True) -> None:
        self._speech_generation += 1
        self._speech_queue.clear()
        task = self._speech_task
        self._speech_task = None
        if task is not None and not task.done():
            task.cancel()
        if show_status:
            self.statusBar().showMessage("Spoken response stopped.", 1500)

    def _cancel_voice_pipeline(self) -> None:
        task = self._voice_pipeline_task
        self._voice_pipeline_task = None
        if task is not None and not task.done():
            task.cancel()

    def _cancel_interrupt_probe(self) -> None:
        task = getattr(self, "_voice_interrupt_probe_task", None)
        self._voice_interrupt_probe_task = None
        if task is not None and not task.done():
            task.cancel()

    def _cancel_assistant_responses(self) -> None:
        for task in list(self._assistant_tasks):
            if not task.done():
                task.cancel()

    def _arm_voice_capture(self) -> None:
        mode = self._interrupt_mode()
        target = self._ring_voice_target_value() if hasattr(self, "cfg") else "assistant"
        guarded = target == "assistant" and self._assistant_response_active() and mode in {"word", "off"}
        if not guarded:
            self._stop_speech(show_status=False)
        self._set_voice_status("Voice: ready")

    def _begin_voice_capture(self, force: bool = False, session_id: int | None = None) -> None:
        if self._voice_capture_active and not force:
            if session_id is None or session_id == self._voice_session_id:
                return
        self._voice_capture_active = True
        self._voice_session_id = session_id
        self._voice_capture_started_at = time.perf_counter()
        self._voice_capture_generation = getattr(self, "_voice_capture_generation", 0) + 1
        mode = self._interrupt_mode()
        target = self._ring_voice_target_value() if hasattr(self, "cfg") else "assistant"
        guarded = target == "assistant" and self._assistant_response_active() and mode in {"word", "off"}
        self._capture_waiting_for_interrupt_word = guarded
        if guarded:
            status = "Voice: listening for interrupt phrase" if mode == "word" else "Voice: response protected"
            self._set_voice_status(status)
            return
        self._voice_turn += 1
        self._response_generation += 1
        self._stop_speech(show_status=False)
        self._cancel_voice_pipeline()
        self._cancel_assistant_responses()
        if hasattr(self, "prompt"):
            self.prompt.clear()
            self._update_talk_action_state()
        self._set_voice_status("Voice: listening")

    def _schedule_voice_capture_action(self, action: str, payload: dict[str, Any]) -> None:
        session_id = self._voice_session_from_payload(payload)
        mode = self._interrupt_mode()
        guarded = action == "send_audio_to_assistant" and self._assistant_response_active() and mode in {"word", "off"}
        if not self._voice_capture_active:
            self._voice_capture_generation = getattr(self, "_voice_capture_generation", 0) + 1
            self._capture_waiting_for_interrupt_word = guarded
            if not guarded:
                self._voice_turn += 1
                self._response_generation += 1
            self._voice_session_id = session_id
        elif session_id is not None and self._voice_session_id is not None and session_id != self._voice_session_id:
            self._voice_capture_generation = getattr(self, "_voice_capture_generation", 0) + 1
            self._capture_waiting_for_interrupt_word = guarded
            if not guarded:
                self._voice_turn += 1
                self._response_generation += 1
            self._voice_session_id = session_id
        self._voice_capture_active = False
        turn = self._voice_turn
        capture_generation = self._voice_capture_generation
        routed = dict(payload)
        routed["_voice_turn"] = turn
        routed["_voice_capture_generation"] = capture_generation
        routed["_interrupt_gate"] = mode if self._capture_waiting_for_interrupt_word else ""
        routed["_voice_session_id"] = session_id
        routed["_voice_pipeline_started_at"] = time.perf_counter()
        self._capture_waiting_for_interrupt_word = False

        if guarded and mode == "off":
            self._set_voice_status("Voice: response protected")
            return

        async def _run() -> None:
            try:
                await self.router.dispatch(action, routed)
            except asyncio.CancelledError:
                return
            finally:
                current = asyncio.current_task()
                if self._voice_pipeline_task is current:
                    self._voice_pipeline_task = None
                if getattr(self, "_voice_interrupt_probe_task", None) is current:
                    self._voice_interrupt_probe_task = None

        if guarded:
            self._cancel_interrupt_probe()
            self._voice_interrupt_probe_task = self.loop.create_task(_run())
            return

        self._cancel_interrupt_probe()
        self._cancel_voice_pipeline()
        self._voice_pipeline_task = self.loop.create_task(_run())

    @staticmethod
    def _voice_session_from_payload(payload: dict[str, Any] | None) -> int | None:
        if not isinstance(payload, dict):
            return None
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
        if not isinstance(inner, dict):
            return None
        value = inner.get("session_id")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _voice_turn_from_payload(payload: dict[str, Any] | None) -> int | None:
        if not isinstance(payload, dict):
            return None
        value = payload.get("_voice_turn")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _voice_capture_generation_from_payload(payload: dict[str, Any] | None) -> int | None:
        if not isinstance(payload, dict):
            return None
        value = payload.get("_voice_capture_generation")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _voice_capture_is_current(self, generation: int | None) -> bool:
        return generation is None or generation == self._voice_capture_generation

    def _voice_turn_is_current(self, turn: int | None) -> bool:
        return turn is None or turn == self._voice_turn

    def _response_is_current(self, generation: int) -> bool:
        return generation == self._response_generation

    def _remember_response_text(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        if text.lstrip().startswith(("[Ollama error]", "[OpenAI error]", "[Codex error]", "[OpenCode error]")):
            return
        self._last_response_text = text
        button = getattr(self, "replay_response_btn", None)
        if button is not None:
            button.setEnabled(True)

    def _replay_last_response(self) -> None:
        text = (self._last_response_text or "").strip()
        if not text:
            self.statusBar().showMessage("No response to replay yet.", 2000)
            return
        self._maybe_speak_response(text, remember=False)
        self.statusBar().showMessage("Replaying last response.", 1500)

    def _maybe_speak_response(self, text: str, remember: bool = True) -> None:
        self._save_transcription()
        if remember:
            self._remember_response_text(text)
        if not self.cfg.transcription.speak_responses:
            return
        text = (text or "").strip()
        if not text:
            return
        if text.lstrip().startswith(("[Ollama error]", "[OpenAI error]", "[Codex error]", "[OpenCode error]")):
            return

        generation = self._speech_generation
        if self._speech_queue and self._speech_queue[-1][0] == generation:
            previous_generation, previous_text = self._speech_queue[-1]
            if len(previous_text) + len(text) < 520:
                self._speech_queue[-1] = (previous_generation, f"{previous_text} {text}".strip())
            else:
                self._speech_queue.append((generation, text))
        else:
            self._speech_queue.append((generation, text))
        if self._speech_task is None or self._speech_task.done():
            self._speech_task = self.loop.create_task(self._drain_speech_queue())

    async def _drain_speech_queue(self) -> None:
        while self._speech_queue and self.cfg.transcription.speak_responses:
            generation, text = self._speech_queue.pop(0)
            if generation != self._speech_generation:
                continue
            ok, err = await speak_text(
                text,
                voice=self.cfg.transcription.tts_voice,
                rate=int(self.cfg.transcription.tts_rate or 0),
            )
            if generation != self._speech_generation:
                continue
            if not ok:
                logger.warning("Spoken response failed: %s", err)
                self.statusBar().showMessage(f"Spoken response failed: {err}", 5000)

    def _play_feedback_sound(self, kind: str) -> None:
        async def _run() -> None:
            ok, err = await play_feedback_sound(kind)
            if not ok:
                logger.debug("Feedback sound failed: %s", err)

        self.loop.create_task(_run())

    def _update_talk_action_state(self) -> None:
        prompt = self.prompt.toPlainText().strip() if hasattr(self, "prompt") else ""
        has_transcript = bool((self._last_transcript or "").strip())
        has_any_text = bool(prompt or has_transcript)

        for attr, enabled in (
            ("send_last_btn", has_transcript),
            ("send_codex_btn", has_any_text),
            ("send_opencode_btn", has_any_text),
            ("copy_text_btn", has_any_text),
            ("send_btn", bool(prompt)),
            ("clear_btn", bool(prompt)),
        ):
            widget = getattr(self, attr, None)
            if widget is not None:
                widget.setEnabled(enabled)

    def _ring_is_connected(self) -> bool:
        client = getattr(self.ble, "client", None)
        return bool(client is not None and getattr(client, "is_connected", False))

    def _refresh_mic_button_state(self) -> None:
        button = getattr(self, "listen_btn", None)
        if button is None:
            return
        connected = self._ring_is_connected() or self._ring_connection_state == "connected"
        active = bool(getattr(self, "_voice_ui_active", False))
        if active:
            button.setText("●")
            button.setToolTip("Ring audio is active.")
        elif connected:
            button.setText("◉")
            button.setToolTip("Ring connected. Press the ring button to speak.")
        else:
            button.setText("↻")
            button.setToolTip("Connect the ring.")
        button.setProperty("active", active)
        button.setProperty("connected", connected)
        button.style().unpolish(button)
        button.style().polish(button)

    def _handle_mic_button(self) -> None:
        if self._ring_is_connected() or self._ring_connection_state == "connected":
            self._set_voice_status("Voice ready: press the ring button to speak")
            self.statusBar().showMessage("Ring connected. Press the ring button to speak.", 3000)
            return
        if self._ring_connecting or self._saved_ring_auto_connect_running():
            self.statusBar().showMessage("Ring connection is already in progress.", 2500)
            return
        self._auto_connect_wizpr()

    def _toggle_listen(self) -> None:
        # Legacy button mappings now route to the real ring connection control. Previous layer issue
        self._handle_mic_button()

    async def _toggle_ring_lock_from_button(self) -> None:
        if self.cfg.protect_connected_ring_buttons and self._ring_is_connected():
            self.statusBar().showMessage("Ring button lock action ignored to protect the active connection.", 3000)
            self._append_ring_activity("Connection protection ignored a ring lock action.")
            return
        client = self.ble.client
        if client is None or not client.is_connected:
            self.statusBar().showMessage("Connect the ring before using the lock button action.", 3500)
            self._append_ring_activity("Lock skipped: ring not connected.")
            return
        try:
            await self.ring.lock()
            self.statusBar().showMessage("Ring lock toggled.", 2000)
        except Exception as exc:
            logger.warning("Ring lock action failed: %s", exc)
            self.statusBar().showMessage(f"Ring lock failed: {exc}", 5000)
            self._append_ring_activity(f"Lock failed: {exc}")

    def _start_new_chat_from_button(self) -> None:
        if hasattr(self, "prompt"):
            self.prompt.clear()
        if hasattr(self, "output"):
            self.output.clear()
        self._last_response_text = ""
        replay = getattr(self, "replay_response_btn", None)
        if replay is not None:
            replay.setEnabled(False)
        self._update_talk_action_state()
        self.statusBar().showMessage("New chat ready.", 1800)

    def _edit_last_transcript_from_button(self) -> None:
        text = (self._last_transcript or "").strip()
        if not text:
            self.statusBar().showMessage("No transcript available to edit yet.", 2500)
            return
        self.prompt.setPlainText(text)
        self.prompt.setFocus(QtCore.Qt.OtherFocusReason)
        cursor = self.prompt.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        self.prompt.setTextCursor(cursor)
        self._update_talk_action_state()
        self.statusBar().showMessage("Last transcript ready to edit.", 1800)

    def _send_last_transcript(self) -> None:
        if not self._last_transcript:
            self.statusBar().showMessage("No transcript available yet.", 2000)
            return
        self.prompt.setPlainText(self._last_transcript)
        self._update_talk_action_state()
        self._send_chat()

    def _copy_text_to_clipboard(self, text: str, label: str = "Text") -> bool:
        text = (text or "").strip()
        if not text:
            self.statusBar().showMessage(f"No {label.casefold()} available to copy.", 2500)
            return False
        QtWidgets.QApplication.clipboard().setText(text)
        self.statusBar().showMessage(f"{label} copied to clipboard.", 2000)
        self._set_voice_status(f"Voice: {label.casefold()} copied")
        return True

    def _copy_current_text(self) -> None:
        text = self.prompt.toPlainText().strip() or self._last_transcript.strip()
        label = "Prompt" if self.prompt.toPlainText().strip() else "Transcript"
        self._copy_text_to_clipboard(text, label)

    def _copy_last_transcript(self) -> bool:
        return self._copy_text_to_clipboard(self._last_transcript.strip(), "Transcript")

    async def _send_paste_hotkey(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Paste action currently uses Windows Ctrl+V.")
        script = "$ws = New-Object -ComObject WScript.Shell; Start-Sleep -Milliseconds 80; $ws.SendKeys('^v')"
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        if proc.returncode != 0:
            msg = stderr_b.decode("utf-8", errors="replace").strip()
            if not msg:
                msg = stdout_b.decode("utf-8", errors="replace").strip()
            raise RuntimeError(msg or f"Paste action exited with {proc.returncode}.")

    async def _paste_last_transcript(self) -> None:
        if not self._copy_last_transcript():
            return
        try:
            await self._send_paste_hotkey()
        except Exception as exc:
            self.statusBar().showMessage(f"Paste failed: {exc}", 5000)
            self.output.appendPlainText(f"[paste error] {exc}\n")
            return
        self.statusBar().showMessage("Transcript pasted into the active app.", 2500)

    def _send_current_to_codex(self) -> None:
        async def _run() -> None:
            prompt = self.prompt.toPlainText().strip() or self._last_transcript.strip()
            if not prompt:
                self.statusBar().showMessage("No prompt or transcript to send to Codex.", 2500)
                return
            await self._send_prompt_to_codex(prompt)

        self.loop.create_task(_run())

    def _send_current_to_opencode(self) -> None:
        async def _run() -> None:
            prompt = self.prompt.toPlainText().strip() or self._last_transcript.strip()
            if not prompt:
                self.statusBar().showMessage("No prompt or transcript to send to OpenCode.", 2500)
                return
            await self._send_prompt_to_opencode(prompt)

        self.loop.create_task(_run())

    async def _send_last_to_codex(self) -> None:
        prompt = self._last_transcript.strip() or self.prompt.toPlainText().strip()
        if not prompt:
            self.statusBar().showMessage("No transcript available yet.", 2000)
            return
        await self._send_prompt_to_codex(prompt)

    async def _send_last_to_opencode(self) -> None:
        prompt = self._last_transcript.strip() or self.prompt.toPlainText().strip()
        if not prompt:
            self.statusBar().showMessage("No transcript available yet.", 2000)
            return
        await self._send_prompt_to_opencode(prompt)

    def _audio_path_from_payload(self, payload: dict[str, Any] | None = None) -> Path | None:
        raw_path = ""
        if isinstance(payload, dict):
            inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
            raw_path = str(inner.get("path", "") if isinstance(inner, dict) else "")
        return Path(raw_path) if raw_path else self._last_audio_path

    def _audio_transcript_cache_key(self, audio_path: Path, backend_name: str) -> tuple[Any, ...]:
        try:
            stat = audio_path.stat()
            mtime = stat.st_mtime_ns
            size = stat.st_size
        except OSError:
            mtime = 0
            size = 0

        if backend_name == "openai":
            backend = ("openai", self.cfg.openai.transcription_model)
        else:
            backend = (
                "local",
                self.cfg.transcription.local_model or "small.en",
                self.cfg.transcription.local_compute_type or "int8",
            )
        return (
            str(audio_path.resolve()),
            mtime,
            size,
            backend,
            bool(self.cfg.transcription.audio_preflight_enabled),
            float(self.cfg.transcription.audio_preflight_min_seconds or 0.35),
            float(self.cfg.transcription.audio_preflight_min_rms or 0.0025),
            float(self.cfg.transcription.audio_preflight_min_active_seconds),
        )

    def _remember_audio_transcript(self, key: tuple[Any, ...], text: str) -> None:
        self._audio_transcript_cache[key] = text
        while len(self._audio_transcript_cache) > 32:
            self._audio_transcript_cache.pop(next(iter(self._audio_transcript_cache)))

    def _voice_command_for_target(self, transcript: str, target: str) -> str:
        text = " ".join(transcript.strip().split())
        if not text:
            return ""
        if target == "assistant" and not self._target_requires_wake_phrase(target):
            for phrase in self._wake_phrases_for_target(target):
                command = self._strip_wake_phrase(text, phrase)
                if command:
                    return command
            return text
        if not self._target_requires_wake_phrase(target):
            return text

        for phrase in self._wake_phrases_for_target(target):
            command = self._strip_wake_phrase(text, phrase)
            if command:
                return command
        return "" if target in {"codex", "opencode", "clipboard", "paste"} else text

    def _target_requires_wake_phrase(self, target: str) -> bool:
        if self._target_always_requires_wake_phrase(target):
            return True
        return bool(self.cfg.transcription.require_wake_word)

    def _target_always_requires_wake_phrase(target: str) -> bool:
        return target in {"codex", "opencode", "clipboard", "paste"}

    def _wake_required_status_text(self) -> str:
        target = self._ring_voice_target_value()
        if self._target_always_requires_wake_phrase(target):
            label = self._voice_target_label()
            return f"Voice: {label} wake phrase required"
        return "Voice: wake phrase required" if self.cfg.transcription.require_wake_word else "Voice: wake phrase off"

    def _wake_phrases_for_target(self, target: str) -> list[str]:
        if target == "assistant":
            raw = self.cfg.transcription.assistant_wake_word or "Wizpr, Assistant"
            defaults = {"wizpr", "assistant"}
            aliases = {"wizpr": ["Whisper", "Wisper", "Wizper"]}
        elif target == "codex":
            raw = self.cfg.transcription.codex_wake_word or "Codex"
            defaults = {"codex"}
            aliases = {"codex": ["Code X", "CodeX", "Code"]}
        elif target == "opencode":
            raw = self.cfg.transcription.opencode_wake_word or "OpenCode, Open Code"
            defaults = {"opencode", "open code"}
            aliases = {"opencode": ["OpenCode", "Open Code"], "open code": ["OpenCode", "Open Code"]}
        elif target == "clipboard":
            raw = self.cfg.transcription.clipboard_wake_word or "Wizpr"
            defaults = {"wizpr"}
            aliases = {"wizpr": ["Whisper", "Wisper", "Wizper"]}
        elif target == "paste":
            raw = self.cfg.transcription.paste_wake_word or "Wizpr"
            defaults = {"wizpr"}
            aliases = {"wizpr": ["Whisper", "Wisper", "Wizper"]}
        else:
            return []

        phrases = [part.strip() for part in re.split(r"[,;|]", raw) if part.strip()]
        if not phrases:
            if target == "assistant":
                phrases = ["Wizpr", "Assistant"]
            elif target == "codex":
                phrases = ["Codex"]
            elif target == "opencode":
                phrases = ["OpenCode", "Open Code"]
            else:
                phrases = ["Wizpr"]
        out: list[str] = []
        seen: set[str] = set()
        for phrase in phrases:
            key = " ".join(phrase.casefold().split())
            if key not in seen:
                out.append(phrase)
                seen.add(key)
            if key in defaults:
                for alias in aliases.get(key, []):
                    alias_key = " ".join(alias.casefold().split())
                    if alias_key not in seen:
                        out.append(alias)
                        seen.add(alias_key)
        return out

    def _strip_wake_phrase(text: str, phrase: str) -> str:
        words = phrase.strip().split()
        if not words:
            return ""
        phrase_pattern = r"\s+".join(re.escape(word) for word in words)
        pattern = rf"^(?:hey\s+)?{phrase_pattern}\b(?:[\s,;:\-]+)(.+)$"
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if not match:
            return ""
        return match.group(1).strip()

    def _wake_word_ignore_message(self, target: str, transcript: str) -> str:
        defaults = {"assistant": "Wizpr", "codex": "Codex", "opencode": "OpenCode", "clipboard": "Wizpr", "paste": "Wizpr"}
        phrase = (self._wake_phrases_for_target(target) or [defaults.get(target, "Wizpr")])[0]
        return f"[voice ignored] Say '{phrase}' first to send this transcript automatically: {transcript}\n"

    def _voice_command_signature(target: str, command: str) -> tuple[str, str]:
        norm = re.sub(r"[^a-z0-9']+", " ", command.casefold())
        norm = " ".join(norm.split())
        return target.strip().lower(), norm

    def _is_duplicate_auto_voice_command(self, target: str, command: str, now: float | None = None) -> bool:
        signature = self._voice_command_signature(target, command)
        if not signature[1]:
            return False
        current_time = time.monotonic() if now is None else float(now)
        last_signature = getattr(self, "_last_auto_voice_signature", None)
        last_time = float(getattr(self, "_last_auto_voice_at", 0.0) or 0.0)
        if last_signature != signature:
            return False
        return current_time - last_time < AUTO_VOICE_DUPLICATE_WINDOW_SECONDS

    def _remember_auto_voice_command(self, target: str, command: str, now: float | None = None) -> None:
        self._last_auto_voice_signature = self._voice_command_signature(target, command)
        self._last_auto_voice_at = time.monotonic() if now is None else float(now)

    def _ignore_duplicate_auto_voice_command(self, target: str, command: str) -> None:
        label = {
            "assistant": "Assistant",
            "codex": "Codex",
            "opencode": "OpenCode",
            "clipboard": "Copy Text",
            "paste": "Voice Keyboard",
        }.get(target, target)
        self.output.appendPlainText(f"[voice ignored] Duplicate {label} command skipped: {command}\n")
        self._set_voice_status(f"Voice ignored: duplicate {label} command")
        self.statusBar().showMessage(f"Duplicate {label} voice command skipped.", 4000)

    def _auto_voice_command_rejection_reason(self, command: str) -> str:
        cleaned = clean_transcript(command)
        reason = transcript_rejection_reason(cleaned)
        if reason:
            if self.cfg.transcription.require_wake_word and any(
                part in reason.casefold()
                for part in ("too-short", "short non-command", "filler-only")
            ):
                return ""
            return reason
        if self.cfg.transcription.require_wake_word:
            return ""

        norm = re.sub(r"[^a-z0-9']+", " ", cleaned.casefold())
        words = [word for word in norm.split() if word]
        if not words:
            return "No clear command detected."
        if len(words) == 1:
            return "Ignored one-word automatic command."
        if len(words) <= 2 and not any(len(word) >= 5 for word in words):
            return "Ignored too-short automatic command."
        return ""

    def _ignore_rejected_auto_voice_command(self, target: str, command: str, reason: str) -> None:
        label = {
            "assistant": "Assistant",
            "codex": "Codex",
            "opencode": "OpenCode",
            "clipboard": "Copy Text",
            "paste": "Voice Keyboard",
        }.get(target, target)
        self.output.appendPlainText(f"[voice ignored] {label} command not sent: {reason} ({command})\n")
        self._set_voice_status(f"Voice ignored: {reason}")
        self.statusBar().showMessage(f"{label} voice command not sent: {reason}", 5000)

    def _auto_voice_command_ready(self, target: str, command: str) -> bool:
        if target == "assistant" and clean_transcript(command):
            return True
        reason = self._auto_voice_command_rejection_reason(command)
        if reason:
            self._ignore_rejected_auto_voice_command(target, command, reason)
            return False
        return True

    def _desktop_voice_command_needs_review(command: str) -> bool:
        text = " ".join(command.strip().split())
        if not text:
            return False
        if CODING_VOICE_REVIEW_ACTION_RE.search(text):
            return True
        return bool(CODING_VOICE_REVIEW_APP_RE.search(text) and re.search(r"\b(?:open|launch|start|run|click|press|type|paste|copy)\b", text, flags=re.IGNORECASE))

    def _coding_voice_command_needs_review(command: str) -> bool:
        return MainWindow._desktop_voice_command_needs_review(command)

    def _hold_assistant_voice_command(self, command: str, reason: str = "review") -> None:
        self.prompt.setPlainText(command)
        if reason == "desktop-action":
            detail = "Desktop/app-control voice command needs review before it is sent."
        else:
            detail = "Review it, then click Send to run it."
        self.output.appendPlainText(f"[voice held] Assistant command ready. {detail}\n{command}\n")
        self._set_voice_status("Voice held: Assistant command ready for review")
        self.statusBar().showMessage("Assistant voice command held for review.", 6000)

    def _hold_coding_voice_command(self, target: str, command: str, reason: str = "review") -> None:
        label = "Codex" if target == "codex" else "OpenCode"
        self.prompt.setPlainText(command)
        if reason == "desktop-action":
            detail = "Desktop/app-control voice command needs review before it can run."
        else:
            detail = "Review it, then click Send to run it."
        self.output.appendPlainText(
            f"[voice held] {label} command ready. {detail}\n{command}\n"
        )
        self._set_voice_status(f"Voice held: {label} command ready for review")
        self.statusBar().showMessage(f"{label} voice command held for review.", 6000)

    async def _transcribe_audio_capture_only(self, payload: dict[str, Any] | None = None) -> None:
        turn = self._voice_turn_from_payload(payload)
        audio_path = self._audio_path_from_payload(payload)
        if audio_path is None or not audio_path.exists():
            self.statusBar().showMessage("No captured ring audio found.", 2500)
            return

        self._last_audio_path = audio_path
        transcript = await self._transcribe_audio_file(audio_path)
        if not transcript or not self._voice_turn_is_current(turn):
            return
        self._last_transcript = transcript
        self.prompt.setPlainText(transcript)
        self._update_talk_action_state()

    async def _audio_capture_text_command(self, payload: dict[str, Any] | None, target: str) -> str:
        turn = self._voice_turn_from_payload(payload)
        audio_path = self._audio_path_from_payload(payload)
        if audio_path is None or not audio_path.exists():
            self.statusBar().showMessage("No captured ring audio found.", 2500)
            return ""

        label = {"clipboard": "Copy Text", "paste": "Voice Keyboard"}.get(target, target)
        self._last_audio_path = audio_path
        transcript = await self._transcribe_audio_file(audio_path)
        if not transcript or not self._voice_turn_is_current(turn):
            return ""
        self._last_transcript = transcript
        self.prompt.setPlainText(transcript)
        self._update_talk_action_state()
        command = self._voice_command_for_target(transcript, target)
        if not command:
            self.output.appendPlainText(self._wake_word_ignore_message(target, transcript))
            self._set_voice_status(f"Voice held: missing {label} wake phrase")
            self.statusBar().showMessage(f"Voice transcript held; missing {label} wake phrase.", 5000)
            return ""
        self.prompt.setPlainText(command)
        if not self._auto_voice_command_ready(target, command):
            return ""
        if self._is_duplicate_auto_voice_command(target, command):
            self._ignore_duplicate_auto_voice_command(target, command)
            return ""
        self._remember_auto_voice_command(target, command)
        return command

    async def _copy_audio_capture_to_clipboard(self, payload: dict[str, Any] | None = None) -> None:
        command = await self._audio_capture_text_command(payload, "clipboard")
        if not command:
            return
        if self._copy_text_to_clipboard(command, "Voice text"):
            self.output.appendPlainText(f"[voice clipboard] {command}\n")

    async def _paste_audio_capture_to_active_app(self, payload: dict[str, Any] | None = None) -> None:
        command = await self._audio_capture_text_command(payload, "paste")
        if not command:
            return
        if not self._copy_text_to_clipboard(command, "Voice text"):
            return
        try:
            await self._send_paste_hotkey()
        except Exception as exc:
            self.statusBar().showMessage(f"Voice paste failed: {exc}", 5000)
            self.output.appendPlainText(f"[voice paste error] {exc}\n")
            return
        self.output.appendPlainText(f"[voice paste] {command}\n")
        self.statusBar().showMessage("Voice text pasted into the active app.", 2500)

    async def _send_audio_capture_to_assistant(self, payload: dict[str, Any] | None = None) -> None:
        turn = self._voice_turn_from_payload(payload)
        capture_generation = self._voice_capture_generation_from_payload(payload)
        interrupt_gate = str(payload.get("_interrupt_gate", "") or "") if isinstance(payload, dict) else ""
        audio_path = self._audio_path_from_payload(payload)
        if audio_path is None or not audio_path.exists():
            self.statusBar().showMessage("No captured ring audio found.", 2500)
            return

        self._last_audio_path = audio_path
        pipeline_started = float(payload.get("_voice_pipeline_started_at", 0.0) or 0.0) if isinstance(payload, dict) else 0.0
        transcription_started = time.perf_counter()
        transcript = await self._transcribe_audio_file(audio_path)
        transcription_elapsed = time.perf_counter() - transcription_started
        if pipeline_started:
            self._append_ble_log(
                f"voice timing: transcription {transcription_elapsed:.2f}s; capture-to-text {time.perf_counter() - pipeline_started:.2f}s"
            )
        if not transcript or not self._voice_capture_is_current(capture_generation):
            return

        if interrupt_gate == "off":
            self._set_voice_status("Voice: response protected")
            return

        matched_interrupt, interrupt_remainder = self._interrupt_command_from_transcript(transcript)
        if interrupt_gate == "word":
            if not matched_interrupt:
                self._append_ble_log(f"voice: ignored during response; interrupt phrase not heard: {transcript}")
                self._set_voice_status("Voice: response continues")
                return
            turn = self._commit_interrupt()
            if not interrupt_remainder:
                self.statusBar().showMessage("Response stopped.", 1800)
                self._set_voice_status("Voice: stopped")
                return
            transcript = interrupt_remainder

        if not self._voice_turn_is_current(turn):
            return
        self._last_transcript = transcript
        command = self._voice_command_for_target(transcript, "assistant")
        if not command or not self._auto_voice_command_ready("assistant", command):
            return
        if self._is_duplicate_auto_voice_command("assistant", command):
            self._ignore_duplicate_auto_voice_command("assistant", command)
            return
        self._remember_auto_voice_command("assistant", command)
        if await self._handle_desktop_tool(command):
            return
        if self._desktop_voice_command_needs_review(command):
            self._hold_assistant_voice_command(command, reason="desktop-action")
            return
        self.prompt.clear()
        self._update_talk_action_state()
        if turn is None:
            await self._send_prompt_to_assistant(command)
        else:
            await self._send_prompt_to_assistant(command, voice_turn=turn)

    async def _send_audio_capture_to_codex(self, payload: dict[str, Any] | None = None) -> None:
        turn = self._voice_turn_from_payload(payload)
        audio_path = self._audio_path_from_payload(payload)
        if audio_path is None or not audio_path.exists():
            self.statusBar().showMessage("No captured ring audio found.", 2500)
            return

        self._last_audio_path = audio_path
        transcript = await self._transcribe_audio_file(audio_path)
        if not transcript or not self._voice_turn_is_current(turn):
            return
        self._last_transcript = transcript
        self.prompt.setPlainText(transcript)
        self._update_talk_action_state()
        command = self._voice_command_for_target(transcript, "codex")
        if not command:
            self.output.appendPlainText(self._wake_word_ignore_message("codex", transcript))
            self._set_voice_status("Voice held: missing Codex wake phrase")
            self.statusBar().showMessage("Voice transcript held; missing Codex wake phrase.", 5000)
            return
        self.prompt.setPlainText(command)
        if not self._auto_voice_command_ready("codex", command):
            return
        if self._is_duplicate_auto_voice_command("codex", command):
            self._ignore_duplicate_auto_voice_command("codex", command)
            return
        self._remember_auto_voice_command("codex", command)
        if self._coding_voice_command_needs_review(command):
            self._hold_coding_voice_command("codex", command, reason="desktop-action")
            return
        if self.cfg.transcription.hold_coding_voice_commands:
            self._hold_coding_voice_command("codex", command)
            return
        await self._send_prompt_to_codex(command)

    async def _send_audio_capture_to_opencode(self, payload: dict[str, Any] | None = None) -> None:
        turn = self._voice_turn_from_payload(payload)
        audio_path = self._audio_path_from_payload(payload)
        if audio_path is None or not audio_path.exists():
            self.statusBar().showMessage("No captured ring audio found.", 2500)
            return

        self._last_audio_path = audio_path
        transcript = await self._transcribe_audio_file(audio_path)
        if not transcript or not self._voice_turn_is_current(turn):
            return
        self._last_transcript = transcript
        self.prompt.setPlainText(transcript)
        self._update_talk_action_state()
        command = self._voice_command_for_target(transcript, "opencode")
        if not command:
            self.output.appendPlainText(self._wake_word_ignore_message("opencode", transcript))
            self._set_voice_status("Voice held: missing OpenCode wake phrase")
            self.statusBar().showMessage("Voice transcript held; missing OpenCode wake phrase.", 5000)
            return
        self.prompt.setPlainText(command)
        if not self._auto_voice_command_ready("opencode", command):
            return
        if self._is_duplicate_auto_voice_command("opencode", command):
            self._ignore_duplicate_auto_voice_command("opencode", command)
            return
        self._remember_auto_voice_command("opencode", command)
        if self._coding_voice_command_needs_review(command):
            self._hold_coding_voice_command("opencode", command, reason="desktop-action")
            return
        if self.cfg.transcription.hold_coding_voice_commands:
            self._hold_coding_voice_command("opencode", command)
            return
        await self._send_prompt_to_opencode(command)

    async def _transcribe_audio_file(self, audio_path: Path) -> str:
        lock = getattr(self, "_voice_transcription_lock", None)
        if lock is None:
            self._voice_transcription_lock = asyncio.Lock()
            lock = self._voice_transcription_lock
        if lock.locked():
            self._set_voice_status("Voice: switching to latest capture")
        async with lock:
            return await self._transcribe_audio_file_locked(audio_path)

    async def _transcribe_audio_file_locked(self, audio_path: Path) -> str:
        self._save_transcription()
        key = self._openai_transcription_key()
        backend = self._transcription_backend()
        if backend == "openai" and not key:
            self._append_ble_log("voice: OpenAI transcription selected without a key; using local STT")
            backend = "local"
        cache_key = self._audio_transcript_cache_key(audio_path, backend)
        if cache_key in self._audio_transcript_cache:
            cached = self._audio_transcript_cache[cache_key]
            if cached:
                self._set_voice_status(f"Voice heard: {cached[:90]}")
            else:
                self._set_voice_status("Voice ignored: rejected capture")
            return cached

        self._append_ble_log(f"voice: transcribing {audio_path.name}")
        self._set_voice_status("Voice: understanding")
        if self.cfg.transcription.audio_preflight_enabled:
            reason, metrics = audio_preflight_reason(
                audio_path,
                min_seconds=float(self.cfg.transcription.audio_preflight_min_seconds or 0.35),
                min_rms=float(self.cfg.transcription.audio_preflight_min_rms or 0.0025),
                min_active_seconds=float(self.cfg.transcription.audio_preflight_min_active_seconds or 0.0),
            )
            if reason:
                suffix = ""
                if metrics:
                    suffix = (
                        f" duration={metrics.get('duration_seconds', 0.0):.2f}s"
                        f" rms={metrics.get('rms', 0.0):.4f}"
                        f" active={metrics.get('active_seconds', 0.0):.2f}s"
                    )
                self._append_ble_log(f"voice ignored: {reason}{suffix}")
                self._set_voice_status(f"Voice ignored: {reason}")
                self.statusBar().showMessage(reason, 5000)
                self._remember_audio_transcript(cache_key, "")
                return ""

        if backend == "openai":
            self.cfg.openai.api_key = key
            if hasattr(self, "openai_base"):
                self.cfg.openai.base_url = self.openai_base.text().strip()
            if hasattr(self, "openai_transcription_model"):
                self.cfg.openai.transcription_model = self.openai_transcription_model.text().strip() or "gpt-4o-transcribe"
            self.p_openai.configure(self.cfg.openai.api_key, self.cfg.openai.base_url)

            self._append_ble_log("voice: using OpenAI transcription")
            self._set_voice_status("Voice: understanding")
            self.statusBar().showMessage("Transcribing with OpenAI...", 4000)
            text, err = await self.p_openai.transcribe_audio(
                audio_path,
                model=self.cfg.openai.transcription_model,
                prompt=(
                    f"{OpenAIProvider.transcription_prompt} "
                    f"Interrupt phrases: {self.cfg.transcription.interrupt_word}."
                ),
            )
            if not err:
                text = clean_transcript(text)
                rejected = transcript_rejection_reason(text)
                if rejected:
                    self._append_ble_log(f"voice ignored: {rejected}")
                    self._set_voice_status(f"Voice ignored: {rejected}")
                    self.statusBar().showMessage(rejected, 5000)
                    self._remember_audio_transcript(cache_key, "")
                    return ""
                self._append_ble_log(f"transcript: {text}")
                self._set_voice_status(f"Voice heard: {text[:90]}")
                self.statusBar().showMessage("Audio transcribed.", 2000)
                self._remember_audio_transcript(cache_key, text)
                return text
            self._append_ble_log(f"voice: OpenAI transcription failed: {err}; using local STT")

        local_model = self.cfg.transcription.local_model or "small.en"
        local_compute = self.cfg.transcription.local_compute_type or "int8"
        local_mode = "warm worker" if local_transcription_uses_persistent_worker() else "one-shot worker"
        self._append_ble_log(f"voice: local STT {local_model}/{local_compute} ({local_mode})")
        self._set_voice_status("Voice: understanding")
        self.statusBar().showMessage("Transcribing locally...", 4000)
        local_timeout = local_transcription_request_timeout_seconds()
        local_started = time.perf_counter()
        try:
            text, err = await asyncio.wait_for(
                transcribe_audio_local(
                    audio_path,
                    model_name=local_model,
                    compute_type=local_compute,
                ),
                timeout=local_timeout,
            )
        except asyncio.TimeoutError:
            await close_local_transcriber()
            text = ""
            err = f"Local transcription timed out after {local_timeout:.0f}s."
        local_elapsed = time.perf_counter() - local_started
        if err:
            lowered = err.casefold()
            if "ignored" in lowered or "no clear speech" in lowered:
                self._append_ble_log(f"voice ignored after {local_elapsed:.2f}s: {err}")
                self._set_voice_status(f"Voice ignored: {err}")
                self.statusBar().showMessage(err, 5000)
            else:
                self._append_ble_log(f"voice error after {local_elapsed:.2f}s: {err}")
                self._set_voice_status(f"Voice error: {err}")
                self.statusBar().showMessage(f"Local transcription failed: {err}", 7000)
            self._remember_audio_transcript(cache_key, "")
            return ""
        self._append_ble_log(f"voice: local STT finished in {local_elapsed:.2f}s; transcript: {text}")
        self._set_voice_status(f"Voice heard: {text[:90]}")
        self.statusBar().showMessage("Audio transcribed locally.", 2500)
        self._remember_audio_transcript(cache_key, text)
        return text

    def _append_output_text(self, text: str) -> None:
        if not text:
            return
        output = getattr(self, "output", None)
        if output is None:
            return
        append_stream = getattr(output, "append_stream_text", None)
        if callable(append_stream):
            append_stream(text)
            return
        cursor = output.textCursor()
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        output.setTextCursor(cursor)
        output.ensureCursorVisible()

    def _next_spoken_chunk(buffer: str, force: bool = False) -> tuple[str, str]:
        text = buffer.lstrip()
        if not text:
            return "", ""
        match = re.search(r"(?<=[.!?])(?:\s+|$)", text)
        if match is not None and match.end() >= 8:
            return text[:match.end()].strip(), text[match.end():]
        if len(text) >= 260:
            cut = text.rfind(" ", 120, 260)
            if cut < 0:
                cut = 260
            return text[:cut].strip(), text[cut:]
        if force:
            return text.strip(), ""
        return "", buffer

    async def _stream_provider_response(
        self,
        provider: Any,
        prompt: str,
        model: str,
        temp: float,
        speak_sentences: bool = False,
        voice_turn: int | None = None,
        response_generation: int = 0,
        request_started_at: float | None = None,
    ) -> str:
        stream_generate = getattr(provider, "stream_generate", None)
        first_chunk_at = 0.0

        def active() -> bool:
            return self._voice_turn_is_current(voice_turn) and self._response_is_current(response_generation)

        if not callable(stream_generate):
            response = await provider.generate(prompt, model=model, temperature=temp)
            if not active():
                return ""
            self._append_output_text(response.text)
            if speak_sentences:
                self._maybe_speak_response(response.text, remember=False)
            return response.text

        parts: list[str] = []
        pending: list[str] = []
        pending_size = 0
        speech_buffer = ""
        last_flush = self.loop.time()
        async for chunk in stream_generate(prompt, model=model, temperature=temp):
            if not active():
                return "".join(parts)
            if not chunk:
                continue
            if first_chunk_at == 0.0:
                first_chunk_at = time.perf_counter()
                if request_started_at is not None:
                    self._append_ble_log(f"voice timing: first response token {first_chunk_at - request_started_at:.2f}s after send")
                try:
                    self.statusBar().showMessage("Responding...", 1500)
                except RuntimeError:
                    pass
            parts.append(chunk)
            pending.append(chunk)
            pending_size += len(chunk)
            if speak_sentences:
                speech_buffer += chunk
                while True:
                    spoken, speech_buffer = self._next_spoken_chunk(speech_buffer)
                    if not spoken:
                        break
                    self._maybe_speak_response(spoken, remember=False)
            now = self.loop.time()
            if pending_size >= 96 or now - last_flush >= 0.012:
                self._append_output_text("".join(pending))
                pending.clear()
                pending_size = 0
                last_flush = now
                await asyncio.sleep(0)
        if not active():
            return "".join(parts)
        if pending:
            self._append_output_text("".join(pending))
        if speak_sentences:
            spoken, _remaining = self._next_spoken_chunk(speech_buffer, force=True)
            if spoken:
                self._maybe_speak_response(spoken, remember=False)
        return "".join(parts)

    async def _send_prompt_to_assistant(self, prompt: str, voice_turn: int | None = None) -> None:
        self._active_response_count = int(getattr(self, "_active_response_count", 0) or 0) + 1
        try:
            if not self._voice_turn_is_current(voice_turn):
                return
            response_generation = self._response_generation
            request_started_at = time.perf_counter()
            pid = self.active_llm_id
            provider = self.registry.get(pid)
            provider_label = self._provider_label(pid)
            if not provider:
                self.output.appendPlainText(f"[error] provider not found: {provider_label}")
                return
    
            if pid == "openai":
                model = self.openai_model.currentText().strip() if hasattr(self, "openai_model") else self.cfg.openai.model
                temp = float(self.openai_temp.value()) if hasattr(self, "openai_temp") else 0.7
                self._save_openai()
            elif pid == "ollama":
                model = self.ollama_model.currentText().strip() if hasattr(self, "ollama_model") else self.cfg.ollama.model
                temp = float(self.ollama_temp.value()) if hasattr(self, "ollama_temp") else 0.7
                self._save_ollama()
            else:
                model = self.compat_model.currentText().strip() if hasattr(self, "compat_model") else self.cfg.openai_compat.model
                temp = float(self.compat_temp.value()) if hasattr(self, "compat_temp") else 0.7
                self._save_compat()
    
            self.output.appendPlainText(f"\n> [{provider_label}: {model} | t={temp:.2f}] {prompt}\n")
            self.statusBar().showMessage("Generating response...", 2000)
            self._apply_explicit_memory_command(prompt)
            provider_prompt = self._memory_prompt(prompt)
            if voice_turn is not None:
                provider_prompt = (
                    f"{provider_prompt}\n\n"
                    "Respond directly and conversationally. Start with the answer and keep it brief unless more detail is necessary."
                )
            response_text = await self._stream_provider_response(
                provider,
                provider_prompt,
                model,
                temp,
                speak_sentences=voice_turn is not None and self.cfg.transcription.speak_responses,
                voice_turn=voice_turn,
                response_generation=response_generation,
                request_started_at=request_started_at,
            )
            if not self._voice_turn_is_current(voice_turn) or not self._response_is_current(response_generation):
                return
            if pid == "ollama":
                self._apply_discovered_ollama_url()
            self._append_output_text("\n")
            if voice_turn is None:
                self._maybe_speak_response(response_text)
            else:
                self._remember_response_text(response_text)
            self._last_transcript = prompt
            if (
                self.cfg.memory.enabled
                and response_text.strip()
                and not response_text.lstrip().startswith(("[Ollama error]", "[OpenAI error]"))
            ):
                self.memory.record_turn(prompt, response_text, max_turns=self.cfg.memory.max_saved_turns)
            self._update_talk_action_state()
            self.statusBar().showMessage("Response received.", 2000)
        finally:
            self._active_response_count = max(0, int(getattr(self, "_active_response_count", 1) or 1) - 1)

    async def _send_prompt_to_codex(self, prompt: str) -> None:
        self._save_codex()
        self.output.appendPlainText(f"\n> [codex] {prompt}\n")
        self.statusBar().showMessage("Sending to Codex...", 2000)
        result = await run_codex_prompt(prompt, self.cfg.codex, default_cwd=Path.cwd())
        if result.ok:
            self.output.appendPlainText(result.output or "[Codex completed with no final message.]")
            self._maybe_speak_response(result.output)
            self.statusBar().showMessage("Codex response received.", 3000)
        else:
            self.output.appendPlainText(f"[Codex error] {result.error}\n{result.output}".strip())
            self.statusBar().showMessage(f"Codex failed: {result.error}", 5000)

    async def _send_prompt_to_opencode(self, prompt: str) -> None:
        self._save_opencode()
        self.output.appendPlainText(f"\n> [opencode:{self.cfg.opencode.model}] {prompt}\n")
        self.statusBar().showMessage("Sending to OpenCode...", 2000)
        result = await run_opencode_prompt(prompt, self.cfg.opencode, default_cwd=Path.cwd())
        if result.ok:
            self.output.appendPlainText(result.output or "[OpenCode completed with no output.]")
            self._maybe_speak_response(result.output)
            self.statusBar().showMessage("OpenCode response received.", 3000)
        else:
            self.output.appendPlainText(f"[OpenCode error] {result.error}\n{result.output}".strip())
            self.statusBar().showMessage(f"OpenCode failed: {result.error}", 6000)

    def _send_chat(self) -> None:
        prompt = self.prompt.toPlainText().strip()
        if not prompt:
            return
        matched_interrupt, interrupt_remainder = self._interrupt_command_from_transcript(prompt)
        if self._assistant_response_active() and matched_interrupt and not interrupt_remainder:
            self.prompt.clear()
            self._update_talk_action_state()
            self._commit_interrupt()
            self.statusBar().showMessage("Response stopped.", 1800)
            self._set_voice_status("Voice: stopped")
            return
        self.prompt.clear()
        self._update_talk_action_state()
        self._response_generation += 1
        self._stop_speech(show_status=False)
        self._cancel_assistant_responses()

        async def _run() -> None:
            if await self._handle_desktop_tool(prompt):
                return
            await self._send_prompt_to_assistant(prompt)

        task = self.loop.create_task(_run())
        self._assistant_tasks.add(task)
        task.add_done_callback(self._assistant_tasks.discard)

    def _should_warm_voice_at_startup(self) -> bool:
        if not self.cfg.transcription.warm_at_startup:
            return False
        return self._transcription_backend() == "local" and local_transcription_uses_persistent_worker()

    def _schedule_voice_warmup_at_startup(self) -> None:
        if self._warm_task is not None and not self._warm_task.done():
            return
        if not self._should_warm_voice_at_startup():
            self._warm_task = None
            return
        self._warm_task = self.loop.create_task(self._warm_voice_transcriber())

    async def _warm_voice_transcriber(self) -> None:
        await asyncio.sleep(0.25)
        if not self.cfg.transcription.warm_at_startup:
            return
        if self._ring_connecting or self.ring_connection_status.text() in {"Scanning", "Connecting", "Listening", "Waiting", "Reconnecting"}:
            return
        if self._transcription_backend() != "local":
            return
        if not local_transcription_uses_persistent_worker():
            return
        err = await warm_local_transcriber(
            model_name=self.cfg.transcription.local_model or "small.en",
            compute_type=self.cfg.transcription.local_compute_type or "int8",
        )
        if err:
            logger.warning("Local transcription warmup failed: %s", err)
            return
        logger.info("Local transcription worker is warm: %s", self.cfg.transcription.local_model or "small.en")

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        try:
            save_config(self.app_dir, self.cfg)
        except Exception:
            pass
        try:
            self.bridge.stop()
        except Exception:
            pass
        try:
            if not self.loop.is_closed():
                self._ring_keep_connected = False
                self._ring_manual_disconnect = True
                canceled_ring_tasks = self._cancel_ring_background_tasks()
                for task in (
                    *canceled_ring_tasks,
                    getattr(self, "_warm_task", None),
                    getattr(self, "_ollama_warm_task", None),
                    getattr(self, "_speech_task", None),
                    getattr(self, "_active_llm_status_task", None),
                    getattr(self, "_voice_pipeline_task", None),
                    getattr(self, "_voice_interrupt_probe_task", None),
                    *list(getattr(self, "_assistant_tasks", set())),
                ):
                    if task is not None and not task.done():
                        task.cancel()
                        try:
                            self.loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
                        except Exception:
                            pass
                # close BLE
                try:
                    self.loop.run_until_complete(self.mobile_bridge.stop())
                except Exception:
                    pass
                try:
                    self.loop.run_until_complete(close_local_transcriber())
                except Exception:
                    pass
                try:
                    async def _close_providers() -> None:
                        pending = []
                        for provider in (self.p_openai, self.p_ollama, self.p_compat):
                            close = getattr(provider, "close", None)
                            if not callable(close):
                                continue
                            result = close()
                            if inspect.isawaitable(result):
                                pending.append(result)
                        if pending:
                            await asyncio.gather(*pending, return_exceptions=True)

                    self.loop.run_until_complete(_close_providers())
                except Exception:
                    pass
                try:
                    self.loop.run_until_complete(self.ring.disconnect())
                except Exception:
                    pass
                self.loop.stop()
                self.loop.close()
        except Exception:
            pass
        super().closeEvent(event)
