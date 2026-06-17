"""Config loader — reads and validates .logwhisper.yaml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG = {
    "llm": {
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "api_key": os.environ.get("GEMINI_API_KEY", ""),
        "max_tokens": 1000,
        "timeout_seconds": 30,
    },
    "buffer": {"max_lines": 500, "context_window_seconds": 60},
    "triggers": {
        "rate_spike": {"enabled": True, "threshold": 5, "window_seconds": 10, "cooldown_seconds": 60},
        "new_error_type": {"enabled": True, "cooldown_seconds": 300},
        "critical_keyword": {"enabled": True, "cooldown_seconds": 120, "extra_keywords": []},
    },
    "output": {
        "terminal": True,
        "webhook": {"enabled": False, "url": "", "format": "slack"},
        "jsonl": {"enabled": True, "path": ".logwhisper/alerts.jsonl"},
        "windows_notification": True,
    },
    "watch": {"default_files": []},
    "deps": {"auto_install": False, "skip_check": False},
}


def load(path: Path | str | None = None) -> dict[str, Any]:
    """Load config from file, falling back to defaults."""
    if path is None:
        path = Path.cwd() / ".logwhisper.yaml"
    path = Path(path)

    if not path.exists():
        return DEFAULT_CONFIG.copy()

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    # Merge with defaults
    config = DEFAULT_CONFIG.copy()
    for section, values in raw.items():
        if section in config and isinstance(config[section], dict):
            config[section].update(values or {})
        else:
            config[section] = values

    return config
