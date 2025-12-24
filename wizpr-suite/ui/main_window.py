#This will update as SDK is available..
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.config import AppConfig, load_config, save_config
from ..core.logging_setup import get_logger
from ..core.event_bus import EventBus
from ..core.action_router import ActionRouter
from ..ble.ble_manager import BLEManager, DiscoveredDevice
from ..ble.ring_controller import RingController, RingProfile
from ..llm.registry import ProviderRegistry
from ..llm.providers.openai_provider import OpenAIProvider
from ..llm.providers.ollama_provider import OllamaProvider
from ..llm.providers.openai_compat_provider import OpenAICompatProvider

logger = get_logger("wizpr_suite.ui")


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
        self.timer.setInterval(15)
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


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, app_dir: Path) -> None:
        super().__init__()
        self.setWindowTitle("WizprSuite")
        self.resize(1100, 760)

        self.app_dir = app_dir
        self.cfg: AppConfig = load_config(app_dir)

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.bridge = AsyncBridge(self.loop, self)
        self.bridge.start()

        self.bus = EventBus()
        self.router = ActionRouter()
        self.ble = BLEManager()
        self.ring_profile = RingProfile(address=self.cfg.last_ble_address)
        self.ring = RingController(self.ble, self.bus, self.ring_profile)

        self.registry = ProviderRegistry()
        self._init_providers()

        self._listen_enabled = False
        self._last_transcript = ""

        self._setup_logging_panel()
        self._build_ui()
        self._apply_theme()

        self._register_actions()
        self.loop.create_task(self._wire_bus())


    def _init_providers(self) -> None:
        self.p_openai = OpenAIProvider(api_key=self.cfg.openai.api_key, base_url=self.cfg.openai.base_url)
        self.p_ollama = OllamaProvider(base_url=self.cfg.ollama.base_url)
        self.p_compat = OpenAICompatProvider(base_url=self.cfg.openai_compat.base_url, api_key=self.cfg.openai_compat.api_key)

        self.registry.register(self.p_openai)
        self.registry.register(self.p_ollama)
        self.registry.register(self.p_compat)

        self.active_llm_id = "openai"


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
        central = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # top bar
        top = QtWidgets.QHBoxLayout()
        self.theme_btn = QtWidgets.QPushButton("Toggle Theme")
        self.theme_btn.clicked.connect(self._toggle_theme)
        top.addWidget(self.theme_btn)

        top.addStretch(1)

        self.active_llm_combo = QtWidgets.QComboBox()
        self.active_llm_combo.addItems(self.registry.list_ids())
        self.active_llm_combo.setCurrentText(self.active_llm_id)
        self.active_llm_combo.currentTextChanged.connect(self._on_active_llm_changed)
        top.addWidget(QtWidgets.QLabel("Active LLM:"))
        top.addWidget(self.active_llm_combo)

        root.addLayout(top)

        self.tabs = QtWidgets.QTabWidget()
        root.addWidget(self.tabs, 1)

        self._build_tab_ble()
        self._build_tab_llm()
        self._build_tab_chat()
        self._build_tab_mappings()
        self._build_tab_logs()

        self.setCentralWidget(central)
        self.statusBar().showMessage("Ready", 1500)

    def _build_tab_ble(self) -> None:
        tab = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(tab)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(10)

        # scan/connect row
        row = QtWidgets.QHBoxLayout()
        self.ble_scan_btn = QtWidgets.QPushButton("Scan")
        self.ble_scan_btn.clicked.connect(self._scan_ble)
        row.addWidget(self.ble_scan_btn)

        self.ble_scan_seconds = QtWidgets.QDoubleSpinBox()
        self.ble_scan_seconds.setRange(1.0, 30.0)
        self.ble_scan_seconds.setSingleStep(1.0)
        self.ble_scan_seconds.setValue(5.0)
        row.addWidget(QtWidgets.QLabel("Scan (sec):"))
        row.addWidget(self.ble_scan_seconds)

        row.addStretch(1)

        self.ble_connect_btn = QtWidgets.QPushButton("Connect")
        self.ble_connect_btn.clicked.connect(self._connect_selected_ble)
        row.addWidget(self.ble_connect_btn)

        self.ble_disconnect_btn = QtWidgets.QPushButton("Disconnect")
        self.ble_disconnect_btn.clicked.connect(self._disconnect_ble)
        row.addWidget(self.ble_disconnect_btn)

        lay.addLayout(row)

        # devices table
        self.ble_table = QtWidgets.QTableWidget(0, 3)
        self.ble_table.setHorizontalHeaderLabels(["Name", "Address", "RSSI"])
        self.ble_table.horizontalHeader().setStretchLastSection(True)
        self.ble_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.ble_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.ble_table, 1)

        # gatt inspector
        gbox = QtWidgets.QGroupBox("GATT Inspector")
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
        g_lay.addWidget(QtWidgets.QLabel("Notifications"))
        g_lay.addWidget(self.notify_box, 1)

        lay.addWidget(gbox, 2)

        self.tabs.addTab(tab, "Ring / BLE")

    def _build_tab_llm(self) -> None:
        tab = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(tab)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(10)

        lay.addWidget(self._build_openai_group())
        lay.addWidget(self._build_ollama_group())
        lay.addWidget(self._build_compat_group())
        lay.addStretch(1)

        self.tabs.addTab(tab, "LLM Providers")

    def _build_openai_group(self) -> QtWidgets.QGroupBox:
        gb = QtWidgets.QGroupBox("OpenAI")
        form = QtWidgets.QFormLayout(gb)

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

        h = QtWidgets.QHBoxLayout()
        h.addWidget(self.openai_model, 1)
        h.addWidget(self.openai_fetch)
        w = QtWidgets.QWidget()
        w.setLayout(h)
        form.addRow("Model:", w)

        self.openai_temp = QtWidgets.QDoubleSpinBox()
        self.openai_temp.setRange(0.0, 2.0)
        self.openai_temp.setSingleStep(0.05)
        self.openai_temp.setValue(0.7)
        form.addRow("Temperature:", self.openai_temp)

        btns = QtWidgets.QHBoxLayout()
        self.openai_save = QtWidgets.QPushButton("Save OpenAI Settings")
        self.openai_save.clicked.connect(self._save_openai)
        btns.addWidget(self.openai_save)

        self.openai_health = QtWidgets.QPushButton("Health Check")
        self.openai_health.clicked.connect(lambda: self._health_check("openai"))
        btns.addWidget(self.openai_health)

        btns.addStretch(1)
        bw = QtWidgets.QWidget()
        bw.setLayout(btns)
        form.addRow("", bw)

        return gb

    def _build_ollama_group(self) -> QtWidgets.QGroupBox:
        gb = QtWidgets.QGroupBox("Ollama (local)")
        form = QtWidgets.QFormLayout(gb)

        self.ollama_url = QtWidgets.QLineEdit(self.cfg.ollama.base_url)
        form.addRow("Base URL:", self.ollama_url)

        self.ollama_model = QtWidgets.QComboBox()
        self.ollama_model.setEditable(True)
        self.ollama_model.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        self.ollama_model.addItem(self.cfg.ollama.model)
        self.ollama_model.setCurrentText(self.cfg.ollama.model)

        self.ollama_fetch = QtWidgets.QPushButton("Fetch Models")
        self.ollama_fetch.clicked.connect(self._refresh_ollama_models)

        h = QtWidgets.QHBoxLayout()
        h.addWidget(self.ollama_model, 1)
        h.addWidget(self.ollama_fetch)
        w = QtWidgets.QWidget()
        w.setLayout(h)
        form.addRow("Model:", w)

        self.ollama_temp = QtWidgets.QDoubleSpinBox()
        self.ollama_temp.setRange(0.0, 2.0)
        self.ollama_temp.setSingleStep(0.05)
        self.ollama_temp.setValue(0.7)
        form.addRow("Temperature:", self.ollama_temp)

        btns = QtWidgets.QHBoxLayout()
        self.ollama_save = QtWidgets.QPushButton("Save Ollama Settings")
        self.ollama_save.clicked.connect(self._save_ollama)
        btns.addWidget(self.ollama_save)

        self.ollama_health = QtWidgets.QPushButton("Health Check")
        self.ollama_health.clicked.connect(lambda: self._health_check("ollama"))
        btns.addWidget(self.ollama_health)

        btns.addStretch(1)
        bw = QtWidgets.QWidget()
        bw.setLayout(btns)
        form.addRow("", bw)

        return gb

    def _build_compat_group(self) -> QtWidgets.QGroupBox:
        gb = QtWidgets.QGroupBox("OpenAI-Compatible Server")
        form = QtWidgets.QFormLayout(gb)

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

        h = QtWidgets.QHBoxLayout()
        h.addWidget(self.compat_model, 1)
        h.addWidget(self.compat_fetch)
        w = QtWidgets.QWidget()
        w.setLayout(h)
        form.addRow("Model:", w)

        self.compat_temp = QtWidgets.QDoubleSpinBox()
        self.compat_temp.setRange(0.0, 2.0)
        self.compat_temp.setSingleStep(0.05)
        self.compat_temp.setValue(0.7)
        form.addRow("Temperature:", self.compat_temp)

        btns = QtWidgets.QHBoxLayout()
        self.compat_save = QtWidgets.QPushButton("Save Compat Settings")
        self.compat_save.clicked.connect(self._save_compat)
        btns.addWidget(self.compat_save)

        self.compat_health = QtWidgets.QPushButton("Health Check")
        self.compat_health.clicked.connect(lambda: self._health_check("openai_compat"))
        btns.addWidget(self.compat_health)

        btns.addStretch(1)
        bw = QtWidgets.QWidget()
        bw.setLayout(btns)
        form.addRow("", bw)

        return gb

    def _build_tab_chat(self) -> None:
        tab = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(tab)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(10)

        top = QtWidgets.QHBoxLayout()
        self.listen_btn = QtWidgets.QPushButton("Listen: OFF")
        self.listen_btn.clicked.connect(self._toggle_listen)
        top.addWidget(self.listen_btn)

        self.send_last_btn = QtWidgets.QPushButton("Send Last Transcript")
        self.send_last_btn.clicked.connect(self._send_last_transcript)
        top.addWidget(self.send_last_btn)

        top.addStretch(1)
        lay.addLayout(top)

        self.prompt = QtWidgets.QPlainTextEdit()
        self.prompt.setPlaceholderText("Type prompt here…")
        lay.addWidget(self.prompt, 1)

        send_row = QtWidgets.QHBoxLayout()
        self.send_btn = QtWidgets.QPushButton("Send")
        self.send_btn.clicked.connect(self._send_chat)
        send_row.addWidget(self.send_btn)

        self.clear_btn = QtWidgets.QPushButton("Clear")
        self.clear_btn.clicked.connect(lambda: self.prompt.setPlainText(""))
        send_row.addWidget(self.clear_btn)

        send_row.addStretch(1)
        lay.addLayout(send_row)

        self.output = QtWidgets.QPlainTextEdit()
        self.output.setReadOnly(True)
        lay.addWidget(self.output, 2)

        self.tabs.addTab(tab, "Chat")

    def _build_tab_mappings(self) -> None:
        tab = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(tab)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(10)

        help_lbl = QtWidgets.QLabel("Map ring events/topics to actions. Topics: button_single, button_double, button_long, raw_notify.")
        help_lbl.setWordWrap(True)
        lay.addWidget(help_lbl)

        self.map_table = QtWidgets.QTableWidget(0, 2)
        self.map_table.setHorizontalHeaderLabels(["Trigger Topic", "Action"])
        self.map_table.horizontalHeader().setStretchLastSection(True)
        self.map_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.map_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.map_table, 1)

        row = QtWidgets.QHBoxLayout()
        self.map_trigger = QtWidgets.QLineEdit()
        self.map_trigger.setPlaceholderText("e.g. button_single")
        row.addWidget(self.map_trigger, 2)

        self.map_action = QtWidgets.QComboBox()
        self.map_action.addItems(["toggle_listen", "send_last_transcript", "cycle_llm", "noop"])
        row.addWidget(self.map_action, 2)

        self.map_add = QtWidgets.QPushButton("Add Mapping")
        self.map_add.clicked.connect(self._add_mapping)
        row.addWidget(self.map_add)

        self.map_remove = QtWidgets.QPushButton("Remove Selected")
        self.map_remove.clicked.connect(self._remove_mapping)
        row.addWidget(self.map_remove)

        row.addStretch(1)
        lay.addLayout(row)

        self._reload_mapping_table()

        self.tabs.addTab(tab, "Mappings")

    def _build_tab_logs(self) -> None:
        tab = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(tab)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(10)
        lay.addWidget(self.log_box, 1)
        self.tabs.addTab(tab, "Logs")


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


    def _register_actions(self) -> None:
        async def _toggle_listen(payload: dict[str, Any]) -> None:
            self._toggle_listen()

        async def _send_last(payload: dict[str, Any]) -> None:
            self._send_last_transcript()

        async def _cycle_llm(payload: dict[str, Any]) -> None:
            ids = self.registry.list_ids()
            if not ids:
                return
            cur = self.active_llm_id
            nxt = ids[(ids.index(cur) + 1) % len(ids)] if cur in ids else ids[0]
            self.active_llm_id = nxt
            self.active_llm_combo.setCurrentText(nxt)
            self.statusBar().showMessage(f"Active LLM: {nxt}", 2000)

        async def _noop(payload: dict[str, Any]) -> None:
            return

        self.router.register_action_handler("toggle_listen", _toggle_listen)
        self.router.register_action_handler("send_last_transcript", _send_last)
        self.router.register_action_handler("cycle_llm", _cycle_llm)
        self.router.register_action_handler("noop", _noop)

    async def _wire_bus(self) -> None:
        # Map topics to actions via cfg.mappings (I am not currently sure how this will work as I do not have a ring yet). Updates will follow.
        async def _handle(topic: str, payload: Any) -> None:
            for action, triggers in (self.cfg.mappings or {}).items():
                if topic in triggers:
                    await self.router.dispatch(action, {"action": action, "topic": topic, "payload": payload})

        async def _mk(topic: str):
            async def _h(payload: Any) -> None:
                await _handle(topic, payload)
            return _h

        for topic in ["button_single", "button_double", "button_long"]:
            await self.bus.subscribe(topic, await _mk(topic))

        await self.bus.subscribe("raw_notify", self._on_raw_notify)

    async def _on_raw_notify(self, payload: Any) -> None:
        try:
            self.notify_box.appendPlainText(str(payload))
        except Exception:
            pass

    def _reload_mapping_table(self) -> None:
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
        save_config(self.app_dir, self.cfg)
        self._reload_mapping_table()
        self.statusBar().showMessage("Mapping added.", 1500)

    def _remove_mapping(self) -> None:
        rows = sorted({i.row() for i in self.map_table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        for r in rows:
            trig = self.map_table.item(r, 0).text()
            action = self.map_table.item(r, 1).text()
            if action in (self.cfg.mappings or {}) and trig in self.cfg.mappings[action]:
                self.cfg.mappings[action].remove(trig)
        save_config(self.app_dir, self.cfg)
        self._reload_mapping_table()
        self.statusBar().showMessage("Mapping removed.", 1500)


    def _scan_ble(self) -> None:
        async def _run():
            self.ble_scan_btn.setEnabled(False)
            try:
                secs = float(self.ble_scan_seconds.value())
                self.statusBar().showMessage(f"Scanning BLE ({secs:.0f}s)…", 2000)
                devs = await self.ble.scan(seconds=secs)
                self._fill_ble_table(devs)
                self.statusBar().showMessage(f"Found {len(devs)} device(s).", 2500)
            except Exception as e:
                logger.exception("BLE scan failed")
                self.statusBar().showMessage(f"Scan failed: {e}", 4000)
            finally:
                self.ble_scan_btn.setEnabled(True)
        self.loop.create_task(_run())

    def _fill_ble_table(self, devs: list[DiscoveredDevice]) -> None:
        self.ble_table.setRowCount(0)
        for d in devs:
            r = self.ble_table.rowCount()
            self.ble_table.insertRow(r)
            self.ble_table.setItem(r, 0, QtWidgets.QTableWidgetItem(d.name or "(no name)"))
            self.ble_table.setItem(r, 1, QtWidgets.QTableWidgetItem(d.address))
            self.ble_table.setItem(r, 2, QtWidgets.QTableWidgetItem(str(d.rssi)))

    def _selected_ble_address(self) -> str:
        rows = {i.row() for i in self.ble_table.selectedIndexes()}
        if not rows:
            return ""
        r = sorted(rows)[0]
        item = self.ble_table.item(r, 1)
        return item.text().strip() if item else ""

    def _connect_selected_ble(self) -> None:
        addr = self._selected_ble_address() or self.cfg.last_ble_address
        if not addr:
            self.statusBar().showMessage("Select a device first.", 2000)
            return
        self.ring_profile.address = addr
        self.cfg.last_ble_address = addr
        save_config(self.app_dir, self.cfg)

        async def _run():
            try:
                await self.ring.connect()
                self.statusBar().showMessage(f"Connected: {addr}", 2500)
            except Exception as e:
                logger.exception("Connect failed")
                self.statusBar().showMessage(f"Connect failed: {e}", 5000)
        self.loop.create_task(_run())

    def _disconnect_ble(self) -> None:
        async def _run():
            try:
                await self.ring.disconnect()
                self.statusBar().showMessage("Disconnected", 2000)
            except Exception:
                self.statusBar().showMessage("Disconnected (forced)", 2000)
        self.loop.create_task(_run())

    def _refresh_gatt(self) -> None:
        async def _run():
            try:
                self.gatt_tree.clear()
                summary = await self.ring.gatt_summary()
                for s in summary:
                    s_item = QtWidgets.QTreeWidgetItem([f"{s['uuid']}  {s.get('description','')}".strip(), ""])
                    self.gatt_tree.addTopLevelItem(s_item)
                    for c in s.get("characteristics", []):
                        props = ", ".join(c.get("properties", []))
                        c_item = QtWidgets.QTreeWidgetItem([f"{c['uuid']}  {c.get('description','')}".strip(), props])
                        c_item.setData(0, QtCore.Qt.UserRole, c.get("uuid"))
                        s_item.addChild(c_item)
                self.gatt_tree.expandAll()
                self.statusBar().showMessage("GATT refreshed.", 2000)
            except Exception as e:
                logger.exception("GATT refresh failed")
                self.statusBar().showMessage(f"GATT refresh failed: {e}", 4000)
        self.loop.create_task(_run())

    def _selected_char_uuid(self) -> str:
        item = self.gatt_tree.currentItem()
        if not item:
            return ""
        u = item.data(0, QtCore.Qt.UserRole)
        return str(u) if u else ""

    def _subscribe_selected_char(self) -> None:
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


    def _on_active_llm_changed(self, pid: str) -> None:
        if pid:
            self.active_llm_id = pid

    def _save_openai(self) -> None:
        self.cfg.openai.api_key = self.openai_key.text().strip()
        self.cfg.openai.base_url = self.openai_base.text().strip()
        self.cfg.openai.model = self.openai_model.currentText().strip()
        save_config(self.app_dir, self.cfg)
        self.p_openai.configure(self.cfg.openai.api_key, self.cfg.openai.base_url)
        self.statusBar().showMessage("OpenAI saved.", 1500)

    def _save_ollama(self) -> None:
        self.cfg.ollama.base_url = self.ollama_url.text().strip()
        self.cfg.ollama.model = self.ollama_model.currentText().strip()
        save_config(self.app_dir, self.cfg)
        self.p_ollama.configure(self.cfg.ollama.base_url)
        self.statusBar().showMessage("Ollama saved.", 1500)

    def _save_compat(self) -> None:
        self.cfg.openai_compat.base_url = self.compat_url.text().strip()
        self.cfg.openai_compat.api_key = self.compat_key.text().strip()
        self.cfg.openai_compat.model = self.compat_model.currentText().strip()
        save_config(self.app_dir, self.cfg)
        self.p_compat.configure(self.cfg.openai_compat.base_url, self.cfg.openai_compat.api_key)
        self.statusBar().showMessage("Compat saved.", 1500)

    def _health_check(self, pid: str) -> None:
        async def _run():
            p = self.registry.get(pid)
            if not p:
                return
            ok, msg = await p.is_healthy()
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
                if err:
                    self.statusBar().showMessage(f"Ollama models failed: {err}", 5000)
                    return
                keep = self.ollama_model.currentText().strip()
                self._set_combo_models(self.ollama_model, models, keep)
                self.statusBar().showMessage(f"Loaded {len(models)} models.", 2500)
            finally:
                self.ollama_fetch.setEnabled(True)
        self.loop.create_task(_run())

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


    def _toggle_listen(self) -> None:
        self._listen_enabled = not self._listen_enabled
        self.listen_btn.setText("Listen: ON" if self._listen_enabled else "Listen: OFF")
        self.statusBar().showMessage("Listening enabled" if self._listen_enabled else "Listening disabled", 1500)

    def _send_last_transcript(self) -> None:
        if not self._last_transcript:
            self.statusBar().showMessage("No transcript available yet.", 2000)
            return
        self.prompt.setPlainText(self._last_transcript)
        self._send_chat()

    def _send_chat(self) -> None:
        prompt = self.prompt.toPlainText().strip()
        if not prompt:
            return

        async def _run():
            pid = self.active_llm_id
            provider = self.registry.get(pid)
            if not provider:
                self.output.appendPlainText(f"[error] provider not found: {pid}")
                return

            if pid == "openai":
                model = self.openai_model.currentText().strip()
                temp = float(self.openai_temp.value())
                self._save_openai()
            elif pid == "ollama":
                model = self.ollama_model.currentText().strip()
                temp = float(self.ollama_temp.value())
                self._save_ollama()
            else:
                model = self.compat_model.currentText().strip()
                temp = float(self.compat_temp.value())
                self._save_compat()

            self.output.appendPlainText(f"\n> [{pid}:{model} | t={temp:.2f}] {prompt}\n")
            resp = await provider.generate(prompt, model=model, temperature=temp)
            self.output.appendPlainText(resp.text)
            self._last_transcript = prompt

        self.loop.create_task(_run())


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
                # close BLE
                try:
                    self.loop.run_until_complete(self.ble.disconnect())
                except Exception:
                    pass
                self.loop.stop()
                self.loop.close()
        except Exception:
            pass
        super().closeEvent(event)
