"""Config loader — reads and validates .logwhisper.yaml."""

from __future__ import annotations

import copy
import logging
import math
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
    "privacy": {"project_context": False, "chat_service_logs": False},
    "storage": {"max_bytes": 10_485_760, "backups": 3, "capture_logs": True},
    "buffer": {"max_lines": 500, "context_window_seconds": 60},
    "triggers": {
        "rate_spike": {"enabled": True, "threshold": 5, "window_seconds": 10, "cooldown_seconds": 30},
        "new_error_type": {"enabled": True, "cooldown_seconds": 5},
        "critical_keyword": {"enabled": True, "cooldown_seconds": 30, "extra_keywords": []},
        "http_error": {"enabled": True, "cooldown_seconds": 30},
        "metric_threshold": {"thresholds": {}, "enabled": True, "cooldown_seconds": 60},
        "cluster_formation": {"min_sources": 3, "window_seconds": 30, "enabled": True, "cooldown_seconds": 120},
        "silence_anomaly": {"silence_threshold_seconds": 60, "enabled": True, "cooldown_seconds": 300},
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


def _deep_merge(base: dict, override: dict, prefix: str = "") -> None:
    for key, default in base.items():
        if key not in override:
            continue
        value = override[key]
        field = prefix + key
        if key == "thresholds":
            valid = isinstance(value, dict) and all(
                isinstance(k, str) and (type(v) is int or (type(v) is float and math.isfinite(v))) for k, v in value.items()
            )
        elif isinstance(default, dict):
            if isinstance(value, dict):
                _deep_merge(default, value, field + ".")
                continue
            valid = False
        elif isinstance(default, list):
            valid = isinstance(value, list) and all(isinstance(v, str) for v in value)
        elif isinstance(value, str) and key == "provider":
            valid = value == "gemini"
        elif type(default) is int:
            valid = type(value) is int and value >= (0 if key == "cooldown_seconds" else 1)
            if key == "timeout_seconds":
                valid = type(value) in (int, float) and 0 < value <= 3600
        else:
            valid = type(default) is type(value)
            if valid and isinstance(value, str) and key != "api_key":
                valid = bool(expand_env(value).strip())
        if valid:
            base[key] = expand_env(value)
        else:
            logger.warning("Invalid configuration field %s; using its default", field)


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
                document = f.read(65537)
                if len(document) > 65536:
                    raise ValueError("configuration exceeds size limit")
                raw = yaml.safe_load(document) or {}
        except (yaml.YAMLError, OSError, ValueError, RecursionError) as exc:
            logger.warning("Could not read %s (%s) — using default config", path, type(exc).__name__)
            raw = {}

        if isinstance(raw, dict):
            _deep_merge(config, raw)
        else:
            logger.warning("%s must contain a mapping at the top level — ignoring it", path)

    if not config["llm"].get("api_key"):
        config["llm"]["api_key"] = os.environ.get("GEMINI_API_KEY", "")

    return config
