"""Bounded, process-coordinated log and finding persistence."""
import os
from pathlib import Path

from filelock import FileLock


def append_record(path: Path, record: str, settings: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (record + "\n").encode("utf-8", errors="replace")
    with FileLock(str(path) + ".lock", timeout=5):
        if path.exists() and path.stat().st_size and path.stat().st_size + len(encoded) > settings["max_bytes"]:
            for index in range(settings["backups"], 0, -1):
                previous = path if index == 1 else Path(f"{path}.{index - 1}")
                if previous.exists():
                    previous.replace(f"{path}.{index}")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "ab") as stream:
            stream.write(encoded)
