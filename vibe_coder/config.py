"""Configuration and filesystem paths for VibeCoder.

All user-writable state lives under ~/.config/vibe-coder/.
"""

import json
from pathlib import Path

APP_NAME = "vibe-coder"
CONFIG_DIR = Path.home() / ".config" / APP_NAME
CONFIG_PATH = CONFIG_DIR / "config.json"
MEMORY_PATH = CONFIG_DIR / "memories.json"

DEFAULTS = {
    "server_url": "http://localhost:11434",
    # None means "auto-detect from installed models at startup"
    "model": None,
    # Injected verbatim as the system message. Empty string = no system message.
    "system_message": "",
    "memories_enabled": False,
    # Hard cap on injected memory text. This is the prompt-bloat guard:
    # ~1200 chars is roughly 300 tokens, negligible for local models.
    "memory_max_chars": 1200,
}


def load_config() -> dict:
    """Load config, merging over defaults so missing keys never crash."""
    if not CONFIG_PATH.exists():
        return dict(DEFAULTS)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULTS)
    cfg = dict(DEFAULTS)
    for key, value in data.items():
        if key in cfg:
            cfg[key] = value
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2) + "\n", encoding="utf-8"
    )
