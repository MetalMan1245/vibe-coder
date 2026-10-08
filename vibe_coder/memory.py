"""Persistent user memories.

Flat, explicitly-added entries in JSON. Injection into the prompt is
capped at a character budget (see config.DEFAULTS['memory_max_chars'])
to keep local-model prompt overhead negligible. There is deliberately
NO automatic extraction in Phase 1.
"""

import json
from datetime import datetime, timezone

from vibe_coder.config import MEMORY_PATH


class MemoryStore:
    def __init__(self, path=MEMORY_PATH):
        self.path = path
        self.entries: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict) and "text" in e]
        return []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.entries, indent=2) + "\n", encoding="utf-8"
        )

    def add(self, text: str, tag: str = "general") -> None:
        self.entries.append({
            "text": text.strip(),
            "tag": tag.strip() or "general",
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        self.save()

    def remove(self, index: int) -> None:
        if 0 <= index < len(self.entries):
            self.entries.pop(index)
            self.save()

    def render(self, max_chars: int) -> str:
        """Render memories as a compact bullet block, capped at max_chars.

        Returns "" when there is nothing to inject.
        """
        if not self.entries:
            return ""
        lines = [f"- {e['text']}" for e in self.entries]
        block = "\n".join(lines)
        if len(block) > max_chars:
            block = block[:max_chars].rsplit("\n", 1)[0]
            block += "\n- (memory list truncated at character budget)"
        return block
