"""Ring buffer — fixed-size sliding window of log objects with frequency tracking."""

from __future__ import annotations

import re
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from buffer.parser import is_http_error

# Error-related patterns to also track for signature deduplication
_ERROR_SIGNATURE_RE = re.compile(
    r"(?i)\b(error|exception|failed|failure|fatal|crash|panic|denied|forbidden|unauthorized|refused|not found|cannot find|module not found|undefined|timeout|network error|cannot load|cannot render|failed to load)\b",
)

# Max entries in the signature set before pruning
_SIGNATURE_CAP = 10_000
_SIGNATURE_PRUNE_TO = 5_000

# How many seconds of per-source error timestamps to retain
_ERROR_RETENTION_SECONDS = 120


def _entry_epoch(entry: dict) -> float | None:
    """Epoch seconds of a log object (None if its timestamp is unusable)."""
    epoch = entry.get("epoch")
    if isinstance(epoch, (int, float)):
        return float(epoch)
    try:
        return datetime.fromisoformat(entry["ts"].replace("Z", "+00:00")).timestamp()
    except (ValueError, KeyError, AttributeError, TypeError):
        return None


class RingBuffer:
    """Fixed-size sliding window of the most recent log objects."""

    def __init__(
        self,
        maxlen: int = 500,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """
        Args:
            maxlen: Maximum number of log objects retained.
            clock: Returns "now" as epoch seconds. Defaults to wall-clock time; batch
                analysis overrides it so replayed (old) logs are windowed correctly.
        """
        self._buf: deque[dict] = deque(maxlen=maxlen)
        # Insertion-ordered "set" so the oldest signatures are the ones pruned
        self._signatures: dict[str, None] = {}
        # {source: deque[epoch]} — timestamps of ERROR/FATAL lines only
        self._error_times: dict[str, deque[float]] = {}
        # Last-seen epoch per source
        self._last_seen: dict[str, float] = {}
        # Total pushes ever made (monotonic, unlike len() once the deque is full)
        self._total_pushed: int = 0
        self.clock: Callable[[], float] = clock or (lambda: datetime.now(timezone.utc).timestamp())

    # ── Time ──────────────────────────────────────────────────────────────────

    def now(self) -> float:
        return self.clock()

    # ── Ingest ────────────────────────────────────────────────────────────────

    def push(self, log_obj: dict) -> None:
        """Add a log object to the buffer."""
        self._buf.append(log_obj)
        self._total_pushed += 1

        source = log_obj["source"]
        level = log_obj.get("level", "INFO")
        epoch = _entry_epoch(log_obj)
        if epoch is None:
            epoch = self.now()

        # ── Error-rate tracking (ERROR/FATAL lines only) ───────────────────────
        if level in ("ERROR", "FATAL"):
            times = self._error_times.setdefault(source, deque())
            times.append(epoch)
            cutoff = epoch - _ERROR_RETENTION_SECONDS
            while times and times[0] < cutoff:
                times.popleft()

        # ── Last-seen per source ───────────────────────────────────────────────
        self._last_seen[source] = epoch

        # ── Signature deduplication tracking ──────────────────────────────────
        raw = log_obj.get("raw", "")
        if (
            level in ("ERROR", "FATAL")
            or _ERROR_SIGNATURE_RE.search(raw)
            or is_http_error(raw)
        ):
            sig = log_obj["signature"]
            self._signatures.pop(sig, None)  # re-insert so recently-seen sigs are pruned last
            self._signatures[sig] = None
            if len(self._signatures) > _SIGNATURE_CAP:
                self._prune_signatures()

    def _prune_signatures(self) -> None:
        """Drop the oldest signatures to avoid unbounded growth."""
        keep = list(self._signatures)[-_SIGNATURE_PRUNE_TO:]
        self._signatures = dict.fromkeys(keep)

    # ── Public API ────────────────────────────────────────────────────────────

    def window(self, seconds: int = 60) -> list[dict]:
        """Return log objects from the last N seconds."""
        cutoff = self.now() - seconds
        result = []
        for entry in self._buf:
            epoch = _entry_epoch(entry)
            # Malformed timestamp — include it to avoid silent data loss
            if epoch is None or epoch >= cutoff:
                result.append(entry)
        return result

    def recent(self, n: int = 100) -> list[dict]:
        """Return the last n log objects (oldest first)."""
        if n <= 0:
            return []
        entries = list(self._buf)
        return entries[-n:]

    @property
    def total_pushed(self) -> int:
        return self._total_pushed

    def since(self, seq: int) -> tuple[list[dict], int]:
        """Return log objects pushed after sequence number `seq`, and the new sequence number.

        Entries already evicted from the ring are skipped silently.
        """
        missing = self._total_pushed - seq
        if missing <= 0:
            return [], self._total_pushed
        entries = list(self._buf)
        return entries[-min(missing, len(entries)):], self._total_pushed

    def error_rate(self, source: str, window_seconds: int = 10) -> int:
        """Return the number of ERROR/FATAL lines from `source` in the last N seconds."""
        times = self._error_times.get(source)
        if not times:
            return 0
        cutoff = self.now() - window_seconds
        return sum(1 for t in times if t >= cutoff)

    def is_new_signature(self, sig: str) -> bool:
        return sig not in self._signatures

    def stats(self, source: str) -> dict[str, Any]:
        """Return statistics for a given source."""
        recent = self.window(seconds=60)
        src_lines = [l for l in recent if l["source"] == source]

        error_lines = [l for l in src_lines if l["level"] in ("ERROR", "FATAL")]
        unique_sigs = set(l["signature"] for l in error_lines)

        return {
            "errors_per_10s": self.error_rate(source, 10),
            "unique_error_types": len(unique_sigs),
            "total_lines": len(src_lines),
        }

    def stats_all(self) -> dict[str, Any]:
        """Return aggregate statistics across all sources."""
        recent = self.window(seconds=60)

        errors = [l for l in recent if l["level"] in ("ERROR", "FATAL")]
        unique_sigs = {l["signature"] for l in errors}
        unique_sources = {l["source"] for l in recent}

        return {
            "total_errors": len(errors),
            "unique_error_types": len(unique_sigs),
            "total_lines": len(recent),
            "active_sources": list(unique_sources),
            "source_count": len(unique_sources),
        }

    def last_seen(self, source: str) -> datetime | None:
        epoch = self._last_seen.get(source)
        if epoch is None:
            return None
        return datetime.fromtimestamp(epoch, tz=timezone.utc)

    def __len__(self) -> int:
        return len(self._buf)
