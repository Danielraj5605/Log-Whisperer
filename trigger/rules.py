"""Trigger rules — each evaluates a log object against a condition."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from buffer.parser import is_http_error

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

# Broad error keywords — catches errors from any service (frontend, backend, DB).
# HTTP 4xx/5xx status codes are handled separately by HttpErrorRule.
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


def _keyword_regex(keywords: list[str]) -> re.Pattern:
    """Case-insensitive regex matching keywords that start on a word boundary.

    "oom" must not match inside "room" or "zoom". Keywords may be followed by more word
    characters ("error" -> "errors"), except for the very short ones which need both edges.
    """
    parts = []
    for kw in keywords:
        escaped = re.escape(kw)
        parts.append(rf"(?<!\w){escaped}(?!\w)" if len(kw) <= 4 else rf"(?<!\w){escaped}")
    return re.compile("|".join(parts), re.IGNORECASE)


def _clean_keywords(keywords: Any) -> list[str]:
    if not keywords:
        return []
    if isinstance(keywords, str):
        keywords = [keywords]
    return [k.strip() for k in keywords if isinstance(k, str) and k.strip()]


class BaseRule(ABC):
    """Abstract base for trigger rules."""

    cooldown: int = 60  # seconds

    def __init__(self, cooldown: int | None = None) -> None:
        if cooldown is not None:
            self.cooldown = cooldown

    @property
    def name(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    def evaluate(self, log_obj: dict, buffer: "RingBuffer") -> bool:
        ...


class NewErrorTypeRule(BaseRule):
    """Fires when an error signature appears for the first time this session."""

    cooldown = 5  # Short, so distinct new errors in quick succession each get analyzed

    _ERROR_LINE_RE = _keyword_regex(BROAD_ERROR_KEYWORDS)

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
    """Fires when the error rate for a source reaches the threshold within a time window."""

    cooldown = 30

    def __init__(
        self,
        threshold: int = 5,
        window_seconds: int = 10,
        cooldown: int | None = None,
    ) -> None:
        super().__init__(cooldown)
        self.threshold = threshold
        self.window_seconds = window_seconds

    def evaluate(self, log_obj: dict, buffer: "RingBuffer") -> bool:
        if log_obj["level"] not in ("ERROR", "FATAL"):
            return False
        # The current line is evaluated before it is pushed, so count it explicitly
        rate = buffer.error_rate(log_obj["source"], self.window_seconds) + 1
        return rate >= self.threshold


class CriticalKeywordRule(BaseRule):
    """Fires on critical keyword matches regardless of log level."""

    cooldown = 30

    def __init__(
        self,
        extra_keywords: list[str] | None = None,
        cooldown: int | None = None,
    ) -> None:
        super().__init__(cooldown)
        self._regex = _keyword_regex(list(CRITICAL_KEYWORDS) + _clean_keywords(extra_keywords))

    def evaluate(self, log_obj: dict, buffer: "RingBuffer") -> bool:
        return bool(self._regex.search(log_obj["raw"]))


class MetricThresholdRule(BaseRule):
    """Fires when a parsed metric exceeds its configured threshold."""

    cooldown = 60

    def __init__(
        self,
        thresholds: dict[str, float] | None = None,
        cooldown: int | None = None,
    ) -> None:
        super().__init__(cooldown)
        self.thresholds = thresholds or {
            "latency_ms": 2000,
            "cpu_pct": 90,
            "memory_pct": 85,
        }

    def evaluate(self, log_obj: dict, buffer: "RingBuffer") -> bool:
        fields = log_obj.get("fields", {})
        for metric, limit in self.thresholds.items():
            val = fields.get(metric)
            if isinstance(val, (int, float)) and not isinstance(val, bool) and val > limit:
                return True
        return False


class ClusterFormationRule(BaseRule):
    """Fires when errors appear across multiple sources within a time window."""

    cooldown = 120

    def __init__(
        self,
        min_sources: int = 3,
        window_seconds: int = 30,
        cooldown: int | None = None,
    ) -> None:
        super().__init__(cooldown)
        self.min_sources = min_sources
        self.window_seconds = window_seconds

    def evaluate(self, log_obj: dict, buffer: "RingBuffer") -> bool:
        if log_obj["level"] not in ("ERROR", "FATAL"):
            return False
        recent = buffer.window(seconds=self.window_seconds)
        # The current line is evaluated before it is pushed, so include its source
        sources_with_errors = {log_obj["source"]}
        sources_with_errors.update(
            l["source"] for l in recent if l["level"] in ("ERROR", "FATAL")
        )
        return len(sources_with_errors) >= self.min_sources


class HttpErrorRule(BaseRule):
    """Fires when a new HTTP error status code (4xx/5xx) appears in a log line.

    Matches lines like:
      Flask:    127.0.0.1 - - [11/Apr/2026] "POST /predict HTTP/1.1" 500 -
      Express:  POST /api/submit 401 50ms
      Apache:   192.168.1.1 - - [11/Apr/2026] "GET / HTTP/1.1" 500 1234
    """

    cooldown = 30

    def evaluate(self, log_obj: dict, buffer: "RingBuffer") -> bool:
        if not is_http_error(log_obj.get("raw", "")):
            return False
        return buffer.is_new_signature(log_obj["signature"])


class SilenceAnomalyRule(BaseRule):
    """Fires when a source resumes logging after being silent for too long.

    Rules are only evaluated when a line arrives, so the silence is detected on the first
    line *after* the gap (the buffer has not yet recorded that line).
    """

    cooldown = 300  # 5 min

    def __init__(
        self,
        silence_threshold_seconds: int = 60,
        cooldown: int | None = None,
    ) -> None:
        super().__init__(cooldown)
        self.silence_threshold_seconds = silence_threshold_seconds

    def evaluate(self, log_obj: dict, buffer: "RingBuffer") -> bool:
        last_seen = buffer.last_seen(log_obj["source"])
        if last_seen is None:
            return False
        elapsed = buffer.now() - last_seen.timestamp()
        return elapsed >= self.silence_threshold_seconds


def build_rules(trigger_cfg: dict[str, Any] | None = None) -> list[BaseRule]:
    """Build the rule list from the `triggers` section of the config.

    Each rule can be disabled with `enabled: false` and given its own `cooldown_seconds`.
    """
    cfg = trigger_cfg or {}

    def section(name: str) -> dict[str, Any]:
        value = cfg.get(name)
        return value if isinstance(value, dict) else {}

    def enabled(name: str) -> bool:
        return bool(section(name).get("enabled", True))

    def cooldown(name: str) -> int | None:
        value = section(name).get("cooldown_seconds")
        return int(value) if isinstance(value, (int, float)) else None

    rules: list[BaseRule] = []

    if enabled("new_error_type"):
        rules.append(NewErrorTypeRule(cooldown=cooldown("new_error_type")))

    if enabled("rate_spike"):
        s = section("rate_spike")
        rules.append(
            RateSpikeRule(
                threshold=int(s.get("threshold", 5)),
                window_seconds=int(s.get("window_seconds", 10)),
                cooldown=cooldown("rate_spike"),
            )
        )

    if enabled("critical_keyword"):
        rules.append(
            CriticalKeywordRule(
                extra_keywords=section("critical_keyword").get("extra_keywords"),
                cooldown=cooldown("critical_keyword"),
            )
        )

    if enabled("http_error"):
        rules.append(HttpErrorRule(cooldown=cooldown("http_error")))

    if enabled("metric_threshold"):
        thresholds = section("metric_threshold").get("thresholds")
        rules.append(
            MetricThresholdRule(
                thresholds=thresholds if isinstance(thresholds, dict) and thresholds else None,
                cooldown=cooldown("metric_threshold"),
            )
        )

    if enabled("cluster_formation"):
        s = section("cluster_formation")
        rules.append(
            ClusterFormationRule(
                min_sources=int(s.get("min_sources", 3)),
                window_seconds=int(s.get("window_seconds", 30)),
                cooldown=cooldown("cluster_formation"),
            )
        )

    if enabled("silence_anomaly"):
        s = section("silence_anomaly")
        rules.append(
            SilenceAnomalyRule(
                silence_threshold_seconds=int(s.get("silence_threshold_seconds", 60)),
                cooldown=cooldown("silence_anomaly"),
            )
        )

    return rules
