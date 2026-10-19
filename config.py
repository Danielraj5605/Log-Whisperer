"""Config loader — reads and validates .logwhisper.yaml."""

from __future__ import annotations

import copy
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


DEFAULT_CONFIG: dict[str, Any] = {
    "llm": {
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "api_key": "",
        "max_tokens": 2048,
        "timeout_seconds": 30,
    },
    "buffer": {"max_lines": 500, "context_window_seconds": 60},
    "triggers": {
        "rate_spike": {"enabled": True, "threshold": 5, "window_seconds": 10, "cooldown_seconds": 30},
        "new_error_type": {"enabled": True, "cooldown_seconds": 5},
        "critical_keyword": {"enabled": True, "cooldown_seconds": 30, "extra_keywords": []},
        "http_error": {"enabled": True, "cooldown_seconds": 30},
        "metric_threshold": {"enabled": True, "cooldown_seconds": 60},
        "cluster_formation": {"enabled": True, "cooldown_seconds": 120},
        "silence_anomaly": {"enabled": True, "cooldown_seconds": 300},
    },
    "output": {
        "terminal": True,
        "jsonl": {"enabled": True, "path": ".logwhisper/alerts.jsonl"},
        "windows_notification": True,
    },
    "watch": {"default_files": []},
    "deps": {"auto_install": False, "skip_check": False},
}

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env(value: Any) -> Any:
    """Recursively replace ${VAR} references with environment variables ('' if unset)."""
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        elif value is None and isinstance(base.get(key), dict):
            continue  # `section:` with no body — keep defaults
        else:
            base[key] = value
    return base


def load(path: Path | str | None = None) -> dict[str, Any]:
    """Load config from file, falling back to defaults.

    Always returns a fresh dict, so callers may mutate it without affecting DEFAULT_CONFIG.
    """
    config = copy.deepcopy(DEFAULT_CONFIG)

    if path is None:
        path = Path.cwd() / ".logwhisper.yaml"
    path = Path(path)

    if path.is_file():
        try:
            with open(path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
            logger.warning("Could not read %s (%s) — using default config", path, exc)
            raw = {}

        if isinstance(raw, dict):
            _deep_merge(config, expand_env(raw))
        else:
            logger.warning("%s must contain a mapping at the top level — ignoring it", path)

    if not config["llm"].get("api_key"):
        config["llm"]["api_key"] = os.environ.get("GEMINI_API_KEY", "")

    return config
