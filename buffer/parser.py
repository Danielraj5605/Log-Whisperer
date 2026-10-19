"""Log line parser — converts raw log strings into normalized log objects."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

# Timestamp formats to try, in order of preference
_TIMESTAMP_PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "iso8601_tz",
        re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2}))"),
    ),
    (
        "iso8601",
        re.compile(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"),
    ),
    (
        "common_log",
        re.compile(r"(\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4})"),
    ),
    (
        "epoch_ms",
        re.compile(r"(?<![\d.])(\d{13})(?![\d])"),
    ),
    (
        "epoch_s",
        re.compile(r"(?<![\d.])(\d{10}(?:\.\d+)?)(?![\d])"),
    ),
]

# Plausible epoch range (2001-09-09 .. 2100-01-01) so random IDs aren't read as timestamps
_EPOCH_MIN = 1_000_000_000
_EPOCH_MAX = 4_102_444_800

# Level normalization map
_LEVEL_MAP: dict[str, str] = {
    "CRITICAL": "FATAL",
    "CRIT": "FATAL",
    "FATAL": "FATAL",
    "WARNING": "WARN",
    "WARN": "WARN",
    "ERROR": "ERROR",
    "ERR": "ERROR",
    "INFO": "INFO",
    "INFORMATION": "INFO",
    "DEBUG": "DEBUG",
    "TRACE": "DEBUG",
    "VERBOSE": "DEBUG",
}

# Regex to find level keyword anywhere in the log line (word-boundary aware)
_LEVEL_PATTERN = re.compile(
    r"\b(DEBUG|INFO|WARN(?:ING)?|ERROR|ERR|CRIT(?:ICAL)?|FATAL|TRACE|VERBOSE)\b",
    re.IGNORECASE,
)

# Metric key=value extraction (e.g. latency=2847ms cpu=92%)
_METRIC_PATTERN = re.compile(r"\b(\w+)=(\d+(?:\.\d+)?)(ms|%)?(?![\w.])")

# HTTP error detection: a 4xx/5xx status code on a line that looks like an HTTP access/error line
_HTTP_CONTEXT_RE = re.compile(
    r"HTTP/\d|\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b|\bstatus(?:_code)?\b",
    re.IGNORECASE,
)
_HTTP_STATUS_RE = re.compile(r"(?<![\w.:/-])([45]\d\d)(?=\s|-|$|[,;)\"'])")

# Numbers that are NOT 4xx/5xx status codes — collapsed when computing signatures
_VARIABLE_NUMBER_RE = re.compile(r"\b(?![45]\d\d\b)\d+\b")
_VARIABLE_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]+\b|\b[0-9a-fA-F]{8,}\b")


def is_http_error(raw: str) -> bool:
    """True if the line looks like an HTTP log line carrying a 4xx/5xx status."""
    return bool(_HTTP_CONTEXT_RE.search(raw) and _HTTP_STATUS_RE.search(raw))


def _normalize_level(raw: str) -> str:
    upper = raw.upper()
    return _LEVEL_MAP.get(upper, "INFO")


def _parse_timestamp(line: str) -> datetime:
    for name, pattern in _TIMESTAMP_PATTERNS:
        m = pattern.search(line)
        if not m:
            continue
        ts_str = m.group(1)
        try:
            if name == "iso8601_tz":
                dt = datetime.fromisoformat(ts_str.replace(",", ".").replace("Z", "+00:00"))
                return dt.astimezone(timezone.utc)
            if name == "iso8601":
                dt = datetime.fromisoformat(ts_str.replace(",", ".").replace(" ", "T"))
                # Naive timestamps are assumed to be in the machine's local timezone
                return dt.astimezone(timezone.utc)
            if name == "common_log":
                dt = datetime.strptime(ts_str, "%d/%b/%Y:%H:%M:%S %z")
                return dt.astimezone(timezone.utc)
            if name == "epoch_ms":
                value = int(ts_str) / 1000
            else:
                value = float(ts_str)
            if not (_EPOCH_MIN <= value <= _EPOCH_MAX):
                continue
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            continue
    return datetime.now(timezone.utc)


def _extract_fields(line: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for m in _METRIC_PATTERN.finditer(line):
        key = m.group(1).lower()
        raw_val = m.group(2)
        unit = m.group(3)
        val: int | float = float(raw_val) if "." in raw_val else int(raw_val)

        if unit == "ms":
            fields[key.rstrip("_") + "_ms"] = val
        elif unit == "%":
            fields[key.rstrip("_") + "_pct"] = val
        elif key in ("status", "code", "status_code"):
            fields["status"] = int(val)
        else:
            fields[key] = val
    return fields


def _make_signature(level: str, raw: str) -> str:
    """Hash of the line with timestamps, IDs and counters stripped out.

    Two occurrences of the "same" error (differing only in time, request id, pid, ...)
    must share a signature, otherwise every line looks like a brand-new error type.
    """
    text = raw
    for _, pattern in _TIMESTAMP_PATTERNS:
        text = pattern.sub(" ", text)
    text = _VARIABLE_HEX_RE.sub("#", text)
    text = _VARIABLE_NUMBER_RE.sub("#", text)
    text = " ".join(text.split())
    prefix = f"{level.lower()}:{text[:100]}"
    return hashlib.sha1(prefix.encode("utf-8", errors="replace")).hexdigest()[:8]


def parse(line: str, source: str = "unknown", adapter: str = "file") -> dict:
    """
    Parse a raw log line into a normalized log object.

    Args:
        line: Raw log line string.
        source: Source identifier (e.g., filename, container name).
        adapter: Which adapter produced this line (file | docker | stdin | subprocess).

    Returns:
        Normalized log object dict.
    """
    level_match = _LEVEL_PATTERN.search(line)
    level = _normalize_level(level_match.group(1)) if level_match else "INFO"

    ts = _parse_timestamp(line)

    return {
        "ts": ts.isoformat(),
        "epoch": ts.timestamp(),
        "source": source,
        "level": level,
        "raw": line,
        "fields": _extract_fields(line),
        "signature": _make_signature(level, line),
        "adapter": adapter,
    }
