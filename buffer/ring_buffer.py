"""Ring buffer — fixed-size sliding window of log objects with frequency tracking."""

from __future__ import annotations

import re
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

# Error-related patterns to also track for signature deduplication
_ERROR_SIGNATURE_RE = re.compile(
    r"(?i)\b(error|exception|failed|failure|fatal|crash|panic|denied|forbidden|unauthorized|refused|not found|cannot find|module not found|undefined|timeout|network error|400|401|403|404|500|502|503|504|cannot load|cannot render|failed to load)\b",
)

# HTTP error status codes (4xx/5xx) — these indicate issues even without error keywords
_HTTP_ERROR_RE = re.compile(r'(?:"\/|\s)(4\d\d|5\d\d)(?:\s|-|\$)')


class RingBuffer:
    """Fixed-size sliding window of the most recent log objects."""

    def __init__(self, maxlen: int = 500) -> None:
        self._buf: deque[dict] = deque(maxlen=maxlen)
        self._signatures: set[str] = set()
        # {source: {bucket_ts: error_count}}
        self._freq: dict[str, dict[str, int]] = {}
        # Track last-seen timestamp per source
        self._last_seen: dict[str, datetime] = {}

    def push(self, log_obj: dict) -> None:
        """Add a log object to the buffer."""
        self._buf.append(log_obj)

        source = log_obj["source"]
        ts_key = self._ts_bucket(log_obj["ts"])

        if source not in self._freq:
            self._freq[source] = {}
        self._freq[source][ts_key] = self._freq[source].get(ts_key, 0) + 1

        self._last_seen[source] = datetime.fromisoformat(log_obj["ts"].replace("Z", "+00:00")).astimezone(timezone.utc)

        if log_obj["level"] in ("ERROR", "FATAL"):
            self._signatures.add(log_obj["signature"])
        elif _ERROR_SIGNATURE_RE.search(log_obj.get("raw", "")):
            # Also track signatures for non-error-level lines that contain error patterns
            # (e.g. Flask logs like "ERROR in app: Exception on /predict" parse as INFO)
            self._signatures.add(log_obj["signature"])
        elif _HTTP_ERROR_RE.search(log_obj.get("raw", "")):
            # Track signatures for HTTP error status codes (4xx/5xx)
            self._signatures.add(log_obj["signature"])

    def _ts_bucket(self, ts_str: str, bucket_seconds: int = 10) -> str:
        """Return a time bucket key for bucketing by N-second windows."""
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(timezone.utc)
        bucket = int(dt.timestamp() / bucket_seconds) * bucket_seconds
        return str(bucket)

    def window(self, seconds: int = 60) -> list[dict]:
        """Return log objects from the last N seconds."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        cutoff_ts = cutoff.isoformat()
        return [l for l in self._buf if l["ts"] >= cutoff_ts]

    def error_rate(self, source: str, bucket_seconds: int = 10) -> int:
        """Return error count for a source in the last bucket window."""
        if source not in self._freq:
            return 0

        now = datetime.now(timezone.utc)
        current_bucket = int(now.timestamp() / bucket_seconds) * bucket_seconds

        total = 0
        for ts_str, count in self._freq[source].items():
            try:
                bucket = int(float(ts_str))
                if bucket >= current_bucket - bucket_seconds:
                    total += count
            except (ValueError, TypeError):
                continue
        return total

    def is_new_signature(self, sig: str) -> bool:
        return sig not in self._signatures

    def stats(self, source: str) -> dict[str, Any]:
        """Return statistics for a given source."""
        recent = self.window(seconds=60)
        src_lines = [l for l in recent if l["source"] == source]

        error_lines = [l for l in src_lines if l["level"] in ("ERROR", "FATAL")]
        unique_sigs = set(l["signature"] for l in error_lines)

        return {
            "errors_per_10s": self.error_rate(source, bucket_seconds=10),
            "unique_error_types": len(unique_sigs),
            "total_lines": len(src_lines),
        }

    def stats_all(self) -> dict[str, Any]:
        """Return aggregate statistics across all sources."""
        recent = self.window(seconds=60)

        total_errors = sum(
            1 for l in recent if l["level"] in ("ERROR", "FATAL")
        )
        unique_sigs = {
            l["signature"]
            for l in recent
            if l["level"] in ("ERROR", "FATAL")
        }
        unique_sources = {l["source"] for l in recent}

        return {
            "total_errors": total_errors,
            "unique_error_types": len(unique_sigs),
            "total_lines": len(recent),
            "active_sources": list(unique_sources),
            "source_count": len(unique_sources),
        }

    def last_seen(self, source: str) -> datetime | None:
        return self._last_seen.get(source)

    def __len__(self) -> int:
        return len(self._buf)
