"""Trigger rules — each evaluates a log object against a condition."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from buffer.ring_buffer import RingBuffer


CRITICAL_KEYWORDS = [
    "fatal",
    "oom",
    "out of memory",
    "segfault",
    "segmentation fault",
    "panic",
    "kernel panic",
    "connection refused",
    "disk full",
    "no space left",
    "deadlock",
    "stack overflow",
    "core dumped",
]

# Broad error keywords — catches errors from any service (frontend, backend, DB)
BROAD_ERROR_KEYWORDS = [
    # Core errors
    "error",
    "exception",
    "failed",
    "failure",
    "fatal",
    "crash",
    "crashed",
    "panic",
    # Permission/access
    "denied",
    "forbidden",
    "unauthorized",
    "access denied",
    "connection refused",
    "refused",
    # Not found / missing
    "not found",
    "cannot find",
    "module not found",
    "undefined",
    "null reference",
    # Timeouts / network
    "timeout",
    "timed out",
    "network error",
    "enetunreach",
    "econnrefused",
    # HTTP error codes (4xx/5xx)
    " 400 ",
    " 401 ",
    " 403 ",
    " 404 ",
    " 500 ",
    " 502 ",
    " 503 ",
    " 504 ",
    # Frontend-specific
    "cannot load",
    "cannot render",
    "failed to load",
    "react error",
    "errorboundary",
    "unhandled rejection",
    "[vite]",
    # yt-dlp / download errors
    "requested format is not available",
]


class BaseRule(ABC):
    """Abstract base for trigger rules."""

    cooldown: int = 60  # seconds

    @property
    def name(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    def evaluate(self, log_obj: dict, buffer: "RingBuffer") -> bool:
        ...


class NewErrorTypeRule(BaseRule):
    """Fires when an error signature appears for the first time this session."""

    cooldown = 5  # 5 seconds — lowered from 30 to allow per-video notifications

    # Match any error/warning patterns in raw log lines
    _ERROR_LINE_RE = re.compile(
        "|".join(re.escape(k) for k in BROAD_ERROR_KEYWORDS),
        re.IGNORECASE,
    )

    def evaluate(self, log_obj: dict, buffer: "RingBuffer") -> bool:
        level = log_obj["level"]
        raw = log_obj.get("raw", "")

        # Match by parsed level OR by raw line content
        level_match = level in ("ERROR", "FATAL")
        raw_match = bool(self._ERROR_LINE_RE.search(raw))

        if not (level_match or raw_match):
            return False
        return buffer.is_new_signature(log_obj["signature"])


class RateSpikeRule(BaseRule):
    """Fires when error rate exceeds threshold within a time window."""

    cooldown = 30

    def __init__(self, threshold: int = 5, window_seconds: int = 10) -> None:
        self.threshold = threshold
        self.window_seconds = window_seconds

    def evaluate(self, log_obj: dict, buffer: "RingBuffer") -> bool:
        if log_obj["level"] not in ("ERROR", "FATAL"):
            return False
        rate = buffer.error_rate(log_obj["source"], self.window_seconds)
        return rate >= self.threshold


class CriticalKeywordRule(BaseRule):
    """Fires on critical keyword matches regardless of log level."""

    cooldown = 30

    def __init__(self, extra_keywords: list[str] | None = None) -> None:
        all_kw = list(CRITICAL_KEYWORDS)
        if extra_keywords:
            all_kw.extend(extra_keywords)
        pattern = "|".join(re.escape(k) for k in all_kw)
        self._regex = re.compile(pattern, re.IGNORECASE)

    def evaluate(self, log_obj: dict, buffer: "RingBuffer") -> bool:
        return bool(self._regex.search(log_obj["raw"]))


class MetricThresholdRule(BaseRule):
    """Fires when a parsed metric exceeds its configured threshold."""

    cooldown = 60

    def __init__(self, thresholds: dict[str, float] | None = None) -> None:
        self.thresholds = thresholds or {
            "latency_ms": 2000,
            "cpu_pct": 90,
            "memory_pct": 85,
        }

    def evaluate(self, log_obj: dict, buffer: "RingBuffer") -> bool:
        fields = log_obj.get("fields", {})
        for metric, limit in self.thresholds.items():
            val = fields.get(metric)
            if val is not None and val > limit:
                return True
        return False


class ClusterFormationRule(BaseRule):
    """Fires when errors appear across multiple sources within a time window."""

    cooldown = 120

    def __init__(self, min_sources: int = 3, window_seconds: int = 30) -> None:
        self.min_sources = min_sources
        self.window_seconds = window_seconds

    def evaluate(self, log_obj: dict, buffer: "RingBuffer") -> bool:
        if log_obj["level"] not in ("ERROR", "FATAL"):
            return False
        recent = buffer.window(seconds=self.window_seconds)
        sources_with_errors = {
            l["source"] for l in recent if l["level"] in ("ERROR", "FATAL")
        }
        return len(sources_with_errors) >= self.min_sources


class HttpErrorRule(BaseRule):
    """Fires when an HTTP error status code (4xx/5xx) appears in a log line."""

    cooldown = 30  # 30 seconds

    # Match HTTP status codes 400-599 in log lines like:
    # Flask:    127.0.0.1 - - [11/Apr/2026] "POST /predict HTTP/1.1" 500 -
    # Express:  POST /api/submit 401 50ms
    # Apache:   192.168.1.1 - - [11/Apr/2026] "GET / HTTP/1.1" 500 1234
    _HTTP_ERROR_RE = re.compile(r'(?:"\/|\s)(4\d\d|5\d\d)(?:\s|-|\$)')

    def evaluate(self, log_obj: dict, buffer: "RingBuffer") -> bool:
        if not self._HTTP_ERROR_RE.search(log_obj.get("raw", "")):
            return False
        return buffer.is_new_signature(log_obj["signature"])


class SilenceAnomalyRule(BaseRule):
    """Fires when an active source stops emitting logs for too long."""

    cooldown = 300  # 5 min

    def __init__(self, silence_threshold_seconds: int = 60) -> None:
        self.silence_threshold_seconds = silence_threshold_seconds

    def evaluate(self, log_obj: dict, buffer: "RingBuffer") -> bool:
        from datetime import datetime, timedelta, timezone

        last_seen = buffer.last_seen(log_obj["source"])
        if last_seen is None:
            return False
        now = datetime.now(timezone.utc)
        elapsed = (now - last_seen).total_seconds()
        return elapsed >= self.silence_threshold_seconds
