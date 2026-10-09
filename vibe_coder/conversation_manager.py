"""Conversation persistence and management.

Conversations are stored as individual JSON files in ~/.config/vibe-coder/conversations/.
An index.json tracks metadata (title, timestamp, message count) for quick listing.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from vibe_coder.config import CONFIG_DIR

CONVERSATIONS_DIR = CONFIG_DIR / "conversations"
INDEX_PATH = CONVERSATIONS_DIR / "index.json"


class ConversationManager:
    def __init__(self):
        CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
        self._ensure_index()

    def _ensure_index(self) -> None:
        """Create index.json if missing."""
        if not INDEX_PATH.exists():
            self._write_index([])

    def _read_index(self) -> list[dict]:
        try:
            data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return []

    def _write_index(self, entries: list[dict]) -> None:
        INDEX_PATH.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")

    def _get_file_path(self, conv_id: str) -> Path:
        return CONVERSATIONS_DIR / f"{conv_id}.json"

    def create_conversation(self) -> dict:
        """Create a new empty conversation and return its metadata."""
        conv_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry = {
            "id": conv_id,
            "title": "New Conversation",
            "created": now,
            "updated": now,
            "message_count": 0,
        }
        # Write full metadata from the start
        self._save_conversation(conv_id, {
            "id": conv_id,
            "messages": [],
            "created": now,
            "title": "New Conversation",
        })
        self._update_index_entry(entry)
        return entry

    def _normalize_conversation(self, data: dict) -> dict:
        """Ensure a loaded conversation has all required fields."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        defaults = {
            "id": data.get("id", "unknown"),
            "messages": data.get("messages", []),
            "title": data.get("title", "New Conversation"),
            "created": data.get("created", now),
        }
        return defaults

    def _update_index_entry(self, entry: dict) -> None:
        """Insert or update an entry in the index."""
        index = self._read_index()
        existing_idx = next((i for i, e in enumerate(index) if e["id"] == entry["id"]), None)
        if existing_idx is not None:
            index[existing_idx] = entry
        else:
            index.insert(0, entry)
        self._write_index(index)

    def load_conversation(self, conv_id: str) -> dict | None:
        """Load a full conversation by ID."""
        path = self._get_file_path(conv_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        # Normalize to ensure all fields exist
        return self._normalize_conversation(data)

    def save_conversation(self, conv_id: str, messages: list[dict], title: str = None) -> None:
        """Save a conversation's message history."""
        conv_data = self.load_conversation(conv_id)
        if conv_data is None:
            # If it doesn't exist, create fresh metadata
            conv_data = {
                "id": conv_id,
                "messages": [],
                "title": "New Conversation",
                "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }

        conv_data["messages"] = messages
        if title is not None:
            conv_data["title"] = title

        conv_data["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conv_data["message_count"] = len(messages)

        self._save_conversation(conv_id, conv_data)
        self._update_index_entry({
            "id": conv_id,
            "title": conv_data["title"],
            "created": conv_data["created"],
            "updated": conv_data["updated"],
            "message_count": len(messages),
        })

    def _save_conversation(self, conv_id: str, data: dict) -> None:
        """Write conversation JSON to disk."""
        path = self._get_file_path(conv_id)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def delete_conversation(self, conv_id: str) -> bool:
        """Delete a conversation by ID. Returns True if deleted."""
        path = self._get_file_path(conv_id)
        if not path.exists():
            return False

        try:
            path.unlink()
        except OSError:
            return False

        index = self._read_index()
        index = [e for e in index if e["id"] != conv_id]
        self._write_index(index)
        return True

    def list_conversations(self) -> list[dict]:
        """List all conversations sorted by updated descending."""
        index = self._read_index()
        return sorted(index, key=lambda e: e["updated"], reverse=True)

    def calculate_storage_usage(self) -> dict:
        """Calculate total storage used by conversations."""
        total_bytes = 0
        file_count = 0

        for path in CONVERSATIONS_DIR.glob("*.json"):
            if path.name == "index.json":
                continue
            try:
                total_bytes += path.stat().st_size
                file_count += 1
            except OSError:
                continue

        if total_bytes >= 1_000_000:
            size_str = f"{total_bytes / 1_000_000:.1f} MB"
        elif total_bytes >= 1_000:
            size_str = f"{total_bytes / 1_000:.1f} KB"
        else:
            size_str = f"{total_bytes} B"

        return {
            "bytes": total_bytes,
            "formatted": size_str,
            "file_count": file_count,
        }

    def auto_title_conversation(self, conv_id: str, messages: list[dict]) -> str:
        """Generate a title from the first user message (truncated)."""
        user_messages = [m for m in messages if m.get("role") == "user"]
        if not user_messages:
            return "New Conversation"

        first = user_messages[0].get("content", "")[:50].strip()
        if not first:
            return "New Conversation"

        for prefix in ["Help with", "Explain", "Can you", "I need", "What is"]:
            if first.lower().startswith(prefix.lower()):
                first = first[len(prefix):].strip()
                break

        return first[:60] + "..." if len(first) > 60 else first
