"""Helpers for safely rendering LLM findings (whose fields may be missing, null or non-strings)."""

from __future__ import annotations

from typing import Any


def text(finding: dict[str, Any], key: str, default: str = "", limit: int | None = None) -> str:
    """Return finding[key] as a string (default if missing/None), optionally truncated."""
    value = finding.get(key)
    out = default if value is None else str(value)
    return out[:limit] if limit is not None else out


def confidence(finding: dict[str, Any]) -> str:
    """Lower-case confidence ('high' | 'medium' | 'low')."""
    value = text(finding, "confidence", "medium").strip().lower()
    return value if value in ("high", "medium", "low") else "medium"


def items(finding: dict[str, Any], key: str) -> list[str]:
    """Return finding[key] as a list of strings."""
    value = finding.get(key)
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]
