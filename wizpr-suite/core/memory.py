from __future__ import annotations

import json
import re
import time

from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MEMORY_FILE = "memory.json"
_REMEMBER_RE = re.compile(r"^\s*(?:please\s+)?remember(?:\s+that)?\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)
_FORGET_RE = re.compile(r"^\s*(?:please\s+)?forget(?:\s+that)?\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)

@dataclass(frozen=True)
class MemoryStats:
    facts: int
    turns: int

class PersistentMemory:
    def __init__(self, app_dir: Path) -> None:
        self.path = app_dir / _MEMORY_FILE
        self._facts: list[str] = []
        self._turns: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        facts = raw.get("facts", []) if isinstance(raw, dict) else []
        turns = raw.get("turns", []) if isinstance(raw, dict) else []
        if isinstance(facts, list):
            self._facts = [str(item).strip() for item in facts if str(item).strip()]
        if isinstance(turns, list):
            cleaned: list[dict[str, Any]] = []
            for item in turns:
                if not isinstance(item, dict):
                    continue
                user = str(item.get("user", "") or "").strip()
                assistant = str(item.get("assistant", "") or "").strip()
                if not user and not assistant:
                    continue
                cleaned.append(
                    {
                        "user": user,
                        "assistant": assistant,
                        "timestamp": float(item.get("timestamp", 0.0) or 0.0),
                    }
                )
            self._turns = cleaned

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "facts": self._facts, "turns": self._turns}
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.path)

    def _normalized(text: str) -> str:
        return " ".join((text or "").strip().casefold().split())

    def remember_fact(self, fact: str) -> bool:
        fact = " ".join((fact or "").strip().split())
        if not fact:
            return False
        normalized = self._normalized(fact)
        if any(self._normalized(existing) == normalized for existing in self._facts):
            return False
        self._facts.append(fact)
        self._facts = self._facts[-250:]
        self._save()
        return True

    def forget_fact(self, query: str) -> int:
        query_norm = self._normalized(query)
        if not query_norm:
            return 0
        before = len(self._facts)
        self._facts = [fact for fact in self._facts if query_norm not in self._normalized(fact)]
        removed = before - len(self._facts)
        if removed:
            self._save()
        return removed

    def apply_explicit_memory_command(self, text: str) -> tuple[str, str] | None:
        remember = _REMEMBER_RE.match(text or "")
        if remember:
            fact = " ".join(remember.group(1).strip().split())
            changed = self.remember_fact(fact)
            return "remember", fact if changed else ""
        forget = _FORGET_RE.match(text or "")
        if forget:
            query = " ".join(forget.group(1).strip().split())
            removed = self.forget_fact(query)
            return "forget", str(removed)
        return None

    def record_turn(self, user: str, assistant: str, max_turns: int = 200) -> None:
        user = (user or "").strip()
        assistant = (assistant or "").strip()
        if not user and not assistant:
            return
        self._turns.append({"user": user, "assistant": assistant, "timestamp": time.time()})
        self._turns = self._turns[-max(10, int(max_turns)):]
        self._save()

    def clear_history(self) -> None:
        self._turns.clear()
        self._save()

    def clear_all(self) -> None:
        self._facts.clear()
        self._turns.clear()
        self._save()

    def stats(self) -> MemoryStats:
        return MemoryStats(facts=len(self._facts), turns=len(self._turns))

    def context(self, max_recent_turns: int = 12, max_characters: int = 12000) -> str:
        max_recent_turns = max(0, int(max_recent_turns))
        max_characters = max(1000, int(max_characters))
        sections: list[str] = []
        if self._facts:
            facts = "\n".join(f"- {fact}" for fact in self._facts[-80:])
            sections.append(f"Persistent user memories:\n{facts}")
        if max_recent_turns and self._turns:
            lines: list[str] = []
            for turn in self._turns[-max_recent_turns:]:
                user = str(turn.get("user", "") or "").strip()
                assistant = str(turn.get("assistant", "") or "").strip()
                if user:
                    lines.append(f"User: {user}")
                if assistant:
                    lines.append(f"Assistant: {assistant}")
            if lines:
                sections.append("Recent conversation:\n" + "\n".join(lines))
        text = "\n\n".join(sections)
        if len(text) <= max_characters:
            return text
        return text[-max_characters:]
