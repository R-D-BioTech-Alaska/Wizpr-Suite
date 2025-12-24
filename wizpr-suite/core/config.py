from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path

APP_NAME = "WizprSuite"
CONFIG_FILE = "config.json"


class OpenAIConfig:
    api_key: str = ""
    model: str = "gpt-4o-mini"
    base_url: str = ""  # optional


class OllamaConfig:
    base_url: str = "http://127.0.0.1:11434"
    model: str = "llama3.1:8b"


class OpenAICompatConfig:
    base_url: str = "http://127.0.0.1:8080"
    api_key: str = ""
    model: str = ""


class AppConfig:
    theme: str = "dark"  # dark/light
    openai: OpenAIConfig = field(default_factory=OpenAIConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    openai_compat: OpenAICompatConfig = field(default_factory=OpenAICompatConfig)
    last_ble_address: str = ""
    mappings: dict[str, list[str]] | None = None

    def __post_init__(self) -> None:
        if self.mappings is None:
            self.mappings = {
                "toggle_listen": ["button_single"],
                "send_last_transcript": ["button_double"],
                "cycle_llm": ["button_long"],
            }


def get_default_app_dir() -> Path:
    import os
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


def load_config(app_dir: Path) -> AppConfig:
    path = app_dir / CONFIG_FILE
    if not path.exists():
        return AppConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return AppConfig()

    def _dc(dc_cls, val, default):
        if isinstance(val, dict):
            try:
                return dc_cls(**val)
            except Exception:
                return default
        return default

    cfg = AppConfig(
        theme=str(raw.get("theme", "dark")),
        openai=_dc(OpenAIConfig, raw.get("openai"), OpenAIConfig()),
        ollama=_dc(OllamaConfig, raw.get("ollama"), OllamaConfig()),
        openai_compat=_dc(OpenAICompatConfig, raw.get("openai_compat"), OpenAICompatConfig()),
        last_ble_address=str(raw.get("last_ble_address", "")),
        mappings=raw.get("mappings") if isinstance(raw.get("mappings"), dict) else None,
    )
    return cfg

def save_config(app_dir: Path, cfg: AppConfig) -> None:
    app_dir.mkdir(parents=True, exist_ok=True)
    path = app_dir / CONFIG_FILE
    obj = asdict(cfg)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
