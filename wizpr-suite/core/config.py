from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field, fields
from pathlib import Path

APP_NAME = "WizprSuite"
CONFIG_FILE = "config.json"
DEFAULT_MAPPINGS = {
    "toggle_ring_lock": [],
    "start_new_chat": ["button_double"],
    "edit_last_transcript": ["button_triple"],
    "toggle_listen": [],
    "send_last_transcript": [],
    "send_last_to_codex": [],
    "send_audio_to_assistant": ["audio_capture"],
    "send_audio_to_codex": [],
    "send_last_to_opencode": [],
    "send_audio_to_opencode": [],
    "transcribe_audio_only": [],
    "copy_audio_to_clipboard": [],
    "paste_audio_to_active_app": [],
    "copy_last_transcript": [],
    "paste_last_transcript": [],
    "cycle_llm": [],
}
BUTTON_TOPICS = {
    "button_single",
    "button_double",
    "button_triple",
    "button_quad",
    "button_five",
    "button_long",
    "button_multi",
    "sos",
}
BUTTON_MODE_MAPPINGS = {
    "app": {
        "start_new_chat": ["button_double"],
        "edit_last_transcript": ["button_triple"],
    },
    "coding": {
        "toggle_listen": ["button_single"],
        "send_last_transcript": ["button_double"],
        "send_last_to_codex": ["button_triple"],
        "cycle_llm": ["button_long"],
    },
}

class OpenAIConfig:
    api_key: str = ""
    model: str = "gpt-4o-mini"
    transcription_model: str = "gpt-4o-transcribe"
    base_url: str = ""  # optional

class OllamaConfig:
    base_url: str = "http://127.0.0.1:11434"
    model: str = "llama3.1:8b"

class OpenAICompatConfig:
    base_url: str = "http://127.0.0.1:8080"
    api_key: str = ""
    model: str = ""

class CodexConfig:
    executable: str = ""
    model: str = ""
    working_dir: str = ""
    sandbox: str = "workspace-write"
    timeout_seconds: float = 300.0

class OpenCodeConfig:
    executable: str = ""
    model: str = "ollama/qwen36-27b"
    working_dir: str = ""
    timeout_seconds: float = 600.0
    continue_session: bool = True
    auto_approve: bool = False

class TranscriptionConfig:
    voice_pipeline_version: int = 8
    stt_backend: str = "auto"
    local_model: str = "small.en"
    local_compute_type: str = "int8"
    warm_at_startup: bool = True
    warm_after_connect: bool = True
    require_wake_word: bool = False
    assistant_wake_word: str = "Wizpr, Assistant"
    codex_wake_word: str = "Codex"
    opencode_wake_word: str = "OpenCode, Open Code"
    clipboard_wake_word: str = "Wizpr"
    paste_wake_word: str = "Wizpr"
    hold_coding_voice_commands: bool = True
    ring_audio_finalize_delay_ms: int = 180
    ring_audio_idle_finalize_delay_ms: int = 650
    speak_responses: bool = True
    interrupt_mode: str = "word"
    interrupt_word: str = "stop"
    tts_voice: str = ""
    tts_rate: int = 0
    ring_connection_sound: bool = True
    mic_activation_sound: bool = False
    low_battery_warning: bool = True
    voice_mode: str = "proximity"
    ring_sleep_timeout_seconds: int = 5
    audio_preflight_enabled: bool = True
    audio_preflight_min_seconds: float = 0.18
    audio_preflight_min_rms: float = 0.0012
    audio_preflight_min_active_seconds: float = 0.12

class MemoryConfig:
    enabled: bool = True
    max_recent_turns: int = 12
    max_context_characters: int = 12000
    max_saved_turns: int = 200

class ToolConfig:
    permission_mode: str = "ask"

class MobileBridgeConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8844
    token: str = ""
    require_approval: bool = True

class AppConfig:
    theme: str = "dark"  # dark/light
    show_advanced_options: bool = False
    openai: OpenAIConfig = field(default_factory=OpenAIConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    openai_compat: OpenAICompatConfig = field(default_factory=OpenAICompatConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    opencode: OpenCodeConfig = field(default_factory=OpenCodeConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    tools: ToolConfig = field(default_factory=ToolConfig)
    mobile_bridge: MobileBridgeConfig = field(default_factory=MobileBridgeConfig)
    active_llm_id: str = "openai"
    ring_voice_target: str = "assistant"
    button_mode: str = "app"
    auto_connect_saved_ring: bool = True
    protect_connected_ring_buttons: bool = True
    last_ble_address: str = ""
    mappings: dict[str, list[str]] | None = None

    def __post_init__(self) -> None:
        if self.mappings is None:
            self.mappings = {action: list(triggers) for action, triggers in DEFAULT_MAPPINGS.items()}
            _sync_button_mode_mappings(self)
            return
        for action in DEFAULT_MAPPINGS:
            self.mappings.setdefault(action, [])

def get_default_app_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"

def load_config(app_dir: Path) -> AppConfig:
    path = app_dir / CONFIG_FILE
    if not path.exists():
        return AppConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return AppConfig()

    def _dc(dc_cls, val, default):
        if isinstance(val, dict):
            try:
                names = {item.name for item in fields(dc_cls)}
                return dc_cls(**{key: value for key, value in val.items() if key in names})
            except Exception:
                return default
        return default

    raw_mappings = raw.get("mappings") if isinstance(raw.get("mappings"), dict) else None
    raw_button_mode = str(raw.get("button_mode", "") or "").strip().lower()
    if raw_button_mode not in {"app", "coding", "custom"}:
        raw_button_mode = _infer_button_mode(raw_mappings) if raw_mappings is not None else "app"

    cfg = AppConfig(
        theme=str(raw.get("theme", "dark")),
        show_advanced_options=bool(raw.get("show_advanced_options", False)),
        openai=_dc(OpenAIConfig, raw.get("openai"), OpenAIConfig()),
        ollama=_dc(OllamaConfig, raw.get("ollama"), OllamaConfig()),
        openai_compat=_dc(OpenAICompatConfig, raw.get("openai_compat"), OpenAICompatConfig()),
        codex=_dc(CodexConfig, raw.get("codex"), CodexConfig()),
        opencode=_dc(OpenCodeConfig, raw.get("opencode"), OpenCodeConfig()),
        transcription=_dc(TranscriptionConfig, raw.get("transcription"), TranscriptionConfig()),
        memory=_dc(MemoryConfig, raw.get("memory"), MemoryConfig()),
        tools=_dc(ToolConfig, raw.get("tools"), ToolConfig()),
        mobile_bridge=_dc(MobileBridgeConfig, raw.get("mobile_bridge"), MobileBridgeConfig()),
        active_llm_id=str(raw.get("active_llm_id", "") or "openai").strip().lower(),
        ring_voice_target=str(raw.get("ring_voice_target", "") or "").strip().lower(),
        button_mode=raw_button_mode,
        auto_connect_saved_ring=bool(raw.get("auto_connect_saved_ring", True)),
        protect_connected_ring_buttons=bool(raw.get("protect_connected_ring_buttons", True)),
        last_ble_address=str(raw.get("last_ble_address", "")),
        mappings=raw_mappings,
    )
    if cfg.active_llm_id not in {"openai", "ollama", "openai_compat"}:
        cfg.active_llm_id = "openai"
    if cfg.transcription.stt_backend not in {"auto", "local", "openai"}:
        cfg.transcription.stt_backend = "auto"
    transcription_raw = raw.get("transcription")
    if isinstance(transcription_raw, dict):
        pipeline_version = int(transcription_raw.get("voice_pipeline_version", 0) or 0)
        if pipeline_version < 2:
            cfg.transcription.require_wake_word = False
        if pipeline_version < 3:
            cfg.transcription.voice_pipeline_version = 3
            cfg.transcription.ring_audio_idle_finalize_delay_ms = 450
            if float(transcription_raw.get("audio_preflight_min_rms", 0.0025) or 0.0025) >= 0.0025:
                cfg.transcription.audio_preflight_min_rms = 0.0015
            if float(transcription_raw.get("audio_preflight_min_active_seconds", 0.0) or 0.0) == 0.0:
                cfg.transcription.audio_preflight_min_active_seconds = 0.08
            if float(transcription_raw.get("audio_preflight_min_seconds", 0.20) or 0.20) >= 0.20:
                cfg.transcription.audio_preflight_min_seconds = 0.15
        if pipeline_version < 4:
            cfg.transcription.voice_pipeline_version = 4
            cfg.transcription.stt_backend = "auto"
            cfg.transcription.ring_audio_finalize_delay_ms = 180
            cfg.transcription.ring_audio_idle_finalize_delay_ms = 650
            cfg.transcription.mic_activation_sound = False
            cfg.transcription.audio_preflight_min_seconds = 0.08
            cfg.transcription.audio_preflight_min_rms = 0.0008
            cfg.transcription.audio_preflight_min_active_seconds = 0.0
        if pipeline_version < 5:
            cfg.transcription.voice_pipeline_version = 5
            openai_raw = raw.get("openai")
            saved_model = str(openai_raw.get("transcription_model", "") or "") if isinstance(openai_raw, dict) else ""
            if not saved_model or saved_model == "gpt-4o-mini-transcribe":
                cfg.openai.transcription_model = "gpt-4o-transcribe"
        if pipeline_version < 6:
            cfg.transcription.voice_pipeline_version = 6
            cfg.transcription.interrupt_mode = "word"
            if not str(transcription_raw.get("interrupt_word", "") or "").strip():
                cfg.transcription.interrupt_word = "Wizpr stop, Stop Wizpr"
        if pipeline_version < 7:
            cfg.transcription.voice_pipeline_version = 7
            saved_interrupt = " ".join(str(transcription_raw.get("interrupt_word", "") or "").split()).casefold()
            legacy_interrupts = {
                "",
                "wizpr stop",
                "stop wizpr",
                "wizpr stop, stop wizpr",
                "stop wizpr, wizpr stop",
            }
            if saved_interrupt in legacy_interrupts:
                cfg.transcription.interrupt_word = "stop"
        if pipeline_version < 8:
            cfg.transcription.voice_pipeline_version = 8
            saved_min_seconds = float(transcription_raw.get("audio_preflight_min_seconds", 0.08) or 0.08)
            saved_min_rms = float(transcription_raw.get("audio_preflight_min_rms", 0.0008) or 0.0008)
            saved_min_active = float(transcription_raw.get("audio_preflight_min_active_seconds", 0.0) or 0.0)
            if saved_min_seconds <= 0.10:
                cfg.transcription.audio_preflight_min_seconds = 0.18
            if saved_min_rms <= 0.0009:
                cfg.transcription.audio_preflight_min_rms = 0.0012
            if saved_min_active <= 0.01:
                cfg.transcription.audio_preflight_min_active_seconds = 0.12
        old_delay = transcription_raw.get("ring_audio_finalize_delay_ms")
        if old_delay in {250, 350, 500}:
            cfg.transcription.ring_audio_finalize_delay_ms = 180
    if cfg.transcription.interrupt_mode not in {"ring", "word", "both", "off"}:
        cfg.transcription.interrupt_mode = "word"
    if cfg.tools.permission_mode not in {"off", "ask", "allow"}:
        cfg.tools.permission_mode = "ask"
    cfg.memory.max_recent_turns = max(0, min(50, int(cfg.memory.max_recent_turns or 12)))
    cfg.memory.max_context_characters = max(1000, min(50000, int(cfg.memory.max_context_characters or 12000)))
    cfg.memory.max_saved_turns = max(10, min(1000, int(cfg.memory.max_saved_turns or 200)))
    if cfg.ring_voice_target not in {"assistant", "codex", "opencode", "transcript", "clipboard", "paste"}:
        mappings = cfg.mappings or {}
        if "audio_capture" in mappings.get("paste_audio_to_active_app", []) or "audio_capture" in mappings.get("paste_last_transcript", []):
            cfg.ring_voice_target = "paste"
        elif "audio_capture" in mappings.get("copy_audio_to_clipboard", []) or "audio_capture" in mappings.get("copy_last_transcript", []):
            cfg.ring_voice_target = "clipboard"
        elif "audio_capture" in mappings.get("send_audio_to_opencode", []):
            cfg.ring_voice_target = "opencode"
        elif "audio_capture" in mappings.get("transcribe_audio_only", []):
            cfg.ring_voice_target = "transcript"
        elif "audio_capture" in mappings.get("send_audio_to_codex", []):
            cfg.ring_voice_target = "codex"
        else:
            cfg.ring_voice_target = "assistant"
    if cfg.button_mode in BUTTON_MODE_MAPPINGS:
        _sync_button_mode_mappings(cfg)
    _sync_ring_voice_mappings(cfg)
    return cfg

def _infer_button_mode(mappings: dict[str, list[str]]) -> str:
    def has(action: str, topic: str) -> bool:
        return topic in mappings.get(action, [])

    if (
        has("start_new_chat", "button_double")
        and has("edit_last_transcript", "button_triple")
    ):
        return "app"
    if (
        has("toggle_listen", "button_single")
        and has("send_last_transcript", "button_double")
        and has("send_last_to_codex", "button_triple")
    ):
        return "coding"
    return "custom"

def _sync_button_mode_mappings(cfg: AppConfig) -> None:
    mode = (cfg.button_mode or "app").strip().lower()
    preset = BUTTON_MODE_MAPPINGS.get(mode)
    if preset is None:
        cfg.button_mode = "custom"
        return

    mappings = cfg.mappings or {}
    for action, triggers in DEFAULT_MAPPINGS.items():
        mappings.setdefault(action, list(triggers))
    for action, triggers in list(mappings.items()):
        mappings[action] = [topic for topic in triggers if topic not in BUTTON_TOPICS]
    for action, triggers in preset.items():
        mappings.setdefault(action, [])
        for topic in triggers:
            if topic not in mappings[action]:
                mappings[action].append(topic)
    cfg.button_mode = mode
    cfg.mappings = mappings

def _sync_ring_voice_mappings(cfg: AppConfig) -> None:
    mappings = cfg.mappings or {}
    actions = {
        "assistant": "send_audio_to_assistant",
        "codex": "send_audio_to_codex",
        "opencode": "send_audio_to_opencode",
        "transcript": "transcribe_audio_only",
        "clipboard": "copy_audio_to_clipboard",
        "paste": "paste_audio_to_active_app",
    }
    for action in [*actions.values(), "copy_last_transcript", "paste_last_transcript"]:
        mappings.setdefault(action, [])
        mappings[action] = [topic for topic in mappings[action] if topic != "audio_capture"]
    mappings[actions.get(cfg.ring_voice_target, "send_audio_to_assistant")].append("audio_capture")
    cfg.mappings = mappings

def save_config(app_dir: Path, cfg: AppConfig) -> None:
    app_dir.mkdir(parents=True, exist_ok=True)
    path = app_dir / CONFIG_FILE
    obj = asdict(cfg)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
