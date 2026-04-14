"""Cooldown tracker — prevents the same rule from firing repeatedly for the same source."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class CooldownTracker:
    """Tracks the last fire time for each (rule, source) pair."""

    def __init__(self) -> None:
        self._last_fired: dict[tuple[str, str], datetime] = {}

    def is_cooling(self, rule: str, source: str, cooldown_seconds: int) -> bool:
        key = (rule, source)
        last = self._last_fired.get(key)
        if last is None:
            return False
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        return elapsed < cooldown_seconds

    def record(self, rule: str, source: str) -> None:
        key = (rule, source)
        self._last_fired[key] = datetime.now(timezone.utc)

    def reset(self) -> None:
        self._last_fired.clear()
