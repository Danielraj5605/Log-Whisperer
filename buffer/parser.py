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
        re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))"),
    ),
    (
        "iso8601",
        re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"),
    ),
    (
        "epoch_ms",
        re.compile(r"(\d{13})"),
    ),
    (
        "epoch_s",
        re.compile(r"(\d{10}(?:\.\d+)?)"),
    ),
    (
        "common_log",
        re.compile(r"(\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4})"),
    ),
]

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
_METRIC_PATTERN = re.compile(
    r"(\w+)=(?:(\d+(?:\.\d+)?)(ms|%|o)?|true|false)",
    re.IGNORECASE,
)


def _normalize_level(raw: str) -> str:
    upper = raw.upper()
    return _LEVEL_MAP.get(upper, "INFO")


def _parse_timestamp(line: str) -> datetime:
    for name, pattern in _TIMESTAMP_PATTERNS:
        m = pattern.search(line)
        if m:
            ts_str = m.group(1)
            try:
                if name == "iso8601_tz":
                    return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(timezone.utc)
                elif name == "iso8601":
                    try:
                        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc)
                    except ValueError:
                        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
                elif name == "epoch_ms":
                    return datetime.fromtimestamp(int(ts_str) / 1000, tz=timezone.utc)
                elif name == "epoch_s":
                    return datetime.fromtimestamp(float(ts_str), tz=timezone.utc)
            except ValueError:
                continue
    return datetime.now(timezone.utc)


def _extract_fields(line: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for m in _METRIC_PATTERN.finditer(line):
        key = m.group(1).lower()
        raw_val = m.group(2) or m.group(4)
        unit = m.group(3)
        if raw_val is None:
            continue
        try:
            val = float(raw_val) if "." in raw_val else int(raw_val)
        except ValueError:
            val = raw_val

        # Apply unit
        if unit == "ms":
            key = key.rstrip("_") + "_ms"
            fields[key] = val
        elif unit == "%":
            key = key.rstrip("_") + "_pct"
            fields[key] = val
        elif key in ("status", "code", "status_code"):
            fields["status"] = int(val)
        else:
            fields[key] = val
    return fields


def _make_signature(level: str, raw: str) -> str:
    prefix = f"{level.lower()}:{raw[:80]}"
    return hashlib.sha1(prefix.encode()).hexdigest()[:8]


def parse(line: str, source: str = "unknown", adapter: str = "file") -> dict:
    """
    Parse a raw log line into a normalized log object.

    Args:
        line: Raw log line string.
        source: Source identifier (e.g., filename, container name).
        adapter: Which adapter produced this line (file | docker | k8s | stdin).

    Returns:
        Normalized log object dict.
    """
    # Extract level — search the full line (not just the start)
    level_match = _LEVEL_PATTERN.search(line)
    if level_match:
        level = _normalize_level(level_match.group(1))
    else:
        level = "INFO"

    # Extract timestamp
    ts = _parse_timestamp(line)

    # Extract fields
    fields = _extract_fields(line)

    # Signature
    sig = _make_signature(level, line)

    return {
        "ts": ts.isoformat(),
        "source": source,
        "level": level,
        "raw": line,
        "fields": fields,
        "signature": sig,
        "adapter": adapter,
    }
