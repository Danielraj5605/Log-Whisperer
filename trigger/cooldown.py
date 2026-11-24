"""Cooldown tracker — prevents the same rule from firing repeatedly for the same source."""

from __future__ import annotations

import time
from typing import Callable


class CooldownTracker:
    """Tracks the last fire time for each (rule, source) pair."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._last_fired: dict[tuple[str, str], float] = {}

    def is_cooling(self, rule: str, source: str, cooldown_seconds: float) -> bool:
        last = self._last_fired.get((rule, source))
        if last is None:
            return False
        return (self._clock() - last) < cooldown_seconds

    def record(self, rule: str, source: str) -> None:
        self._last_fired[(rule, source)] = self._clock()

    def reset(self) -> None:
        self._last_fired.clear()
