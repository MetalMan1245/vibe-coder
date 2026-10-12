"""Configuration and filesystem paths for VibeCoder.

All user-writable state lives under ~/.config/vibe-coder/.
"""

import json
from pathlib import Path

APP_NAME = "vibe-coder"
CONFIG_DIR = Path.home() / ".config" / APP_NAME
CONFIG_PATH = CONFIG_DIR / "config.json"
MEMORY_PATH = CONFIG_DIR / "memories.json"

# Import DEFAULTS in gui.py needs this
from vibe_coder import config

DEFAULTS = {
    "server_url": "http://localhost:11434",
    # None means "auto-detect from installed models at startup"
    "model": None,
    # Injected verbatim as the system message. Empty string = no system message.
    "system_message": "",
    # AI tools instruction template for projects. Use {project_name}, {scope_path} placeholders.
    "tools_template": (
        "You are working in a project named '{project_name}' at {scope_path}.\n\n"
        "You HAVE ACCESS TO THESE TOOLS that you MUST USE:\n"
        "- read_file(path): Read a file's content\n"
        "- write_file(path, content): Create or overwrite a file (auto-commits)\n"
        "- list_files(recursive=true): List files in the project\n"
        "- delete_file(path): Delete a file\n"
        "- git_commit(message): Stage and commit changes\n\n"
        "When you need to make file changes:\n"
        "1. CALL write_file with the path and content\n"
        "2. The file will be written automatically\n"
        "3. It will be auto-committed with a brief summary\n\n"
        "Do NOT write code blocks or bash commands - USE THE TOOLS DIRECTLY."
    ),
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
