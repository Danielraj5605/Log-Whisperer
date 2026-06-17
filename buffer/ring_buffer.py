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

# Max entries in the signature set before pruning
_SIGNATURE_CAP = 10_000
_SIGNATURE_PRUNE_TO = 5_000

# How often (in pushes) to run the _freq bucket eviction sweep
_EVICT_INTERVAL = 100

# How many seconds of _freq buckets to retain
_FREQ_RETENTION_SECONDS = 120


class RingBuffer:
    """Fixed-size sliding window of the most recent log objects."""

    def __init__(self, maxlen: int = 500) -> None:
        self._buf: deque[dict] = deque(maxlen=maxlen)
        self._signatures: set[str] = set()
        # {source: {bucket_ts_str: error_count}}  — counts ERROR/FATAL lines only
        self._freq: dict[str, dict[str, int]] = {}
        # Track last-seen timestamp per source
        self._last_seen: dict[str, datetime] = {}
        # Counter for periodic maintenance
        self._push_count: int = 0

    def push(self, log_obj: dict) -> None:
        """Add a log object to the buffer."""
        self._buf.append(log_obj)

        source = log_obj["source"]
        level = log_obj.get("level", "INFO")

        # ── Error-rate tracking (ERROR/FATAL lines only) ───────────────────────
        if level in ("ERROR", "FATAL"):
            ts_key = self._ts_bucket(log_obj["ts"])
            if source not in self._freq:
                self._freq[source] = {}
            self._freq[source][ts_key] = self._freq[source].get(ts_key, 0) + 1

        # ── Last-seen per source ───────────────────────────────────────────────
        try:
            self._last_seen[source] = datetime.fromisoformat(
                log_obj["ts"].replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except (ValueError, KeyError):
            self._last_seen[source] = datetime.now(timezone.utc)

        # ── Signature deduplication tracking ──────────────────────────────────
        if level in ("ERROR", "FATAL"):
            self._signatures.add(log_obj["signature"])
        elif _ERROR_SIGNATURE_RE.search(log_obj.get("raw", "")):
            self._signatures.add(log_obj["signature"])
        elif _HTTP_ERROR_RE.search(log_obj.get("raw", "")):
            self._signatures.add(log_obj["signature"])

        # ── Periodic maintenance ───────────────────────────────────────────────
        self._push_count += 1
        if self._push_count % _EVICT_INTERVAL == 0:
            self._prune_signatures()
            self._evict_old_freq_buckets()

    # ── Maintenance helpers ───────────────────────────────────────────────────

    def _prune_signatures(self) -> None:
        """Cap the signature set to avoid unbounded growth."""
        if len(self._signatures) > _SIGNATURE_CAP:
            # Sets are unordered — convert to list, keep the last half
            sigs = list(self._signatures)
            self._signatures = set(sigs[-_SIGNATURE_PRUNE_TO:])

    def _evict_old_freq_buckets(self) -> None:
        """Remove _freq buckets older than _FREQ_RETENTION_SECONDS."""
        now_ts = datetime.now(timezone.utc).timestamp()
        cutoff_bucket = int((now_ts - _FREQ_RETENTION_SECONDS) / 10) * 10

        for source in list(self._freq.keys()):
            self._freq[source] = {
                ts: count
                for ts, count in self._freq[source].items()
                if _safe_int(ts) >= cutoff_bucket
            }
            if not self._freq[source]:
                del self._freq[source]

    # ── Time helpers ──────────────────────────────────────────────────────────

    def _ts_bucket(self, ts_str: str, bucket_seconds: int = 10) -> str:
        """Return a time bucket key for bucketing by N-second windows."""
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(timezone.utc)
            bucket = int(dt.timestamp() / bucket_seconds) * bucket_seconds
            return str(bucket)
        except (ValueError, AttributeError):
            # Fallback: use current time bucket
            bucket = int(datetime.now(timezone.utc).timestamp() / bucket_seconds) * bucket_seconds
            return str(bucket)

    # ── Public API ────────────────────────────────────────────────────────────

    def window(self, seconds: int = 60) -> list[dict]:
        """Return log objects from the last N seconds using datetime comparison."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        result = []
        for entry in self._buf:
            try:
                ts = datetime.fromisoformat(
                    entry["ts"].replace("Z", "+00:00")
                ).astimezone(timezone.utc)
                if ts >= cutoff:
                    result.append(entry)
            except (ValueError, KeyError):
                # Malformed timestamp — include it to avoid silent data loss
                result.append(entry)
        return result

    def error_rate(self, source: str, bucket_seconds: int = 10) -> int:
        """Return ERROR/FATAL line count for a source in the last bucket window."""
        if source not in self._freq:
            return 0

        now = datetime.now(timezone.utc)
        current_bucket = int(now.timestamp() / bucket_seconds) * bucket_seconds

        total = 0
        for ts_str, count in self._freq[source].items():
            bucket = _safe_int(ts_str)
            if bucket is not None and bucket >= current_bucket - bucket_seconds:
                total += count
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


# ── Helpers ───────────────────────────────────────────────────────────────────


def _safe_int(value: str) -> int | None:
    """Parse a string to int, returning None on failure."""
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None
