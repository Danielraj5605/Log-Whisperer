"""File tail adapter — watches a log file and yields new lines as they are appended."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import AsyncIterator

from buffer.parser import parse

_READ_SIZE = 64 * 1024
_MAX_LINE_BYTES = 1024 * 1024  # a "line" longer than this is flushed as-is


class FileAdapter:
    """Tail a file, detecting log rotation/truncation, yielding normalized log objects."""

    def __init__(
        self,
        path: str | Path,
        poll_ms: int = 100,
        source: str | None = None,
        from_beginning: bool = False,
    ) -> None:
        self._path = Path(path)
        self._poll_ms = poll_ms
        self._source = source or self._path.name
        self._running = False
        self._from_beginning = from_beginning

    async def stream(self) -> AsyncIterator[dict]:
        """Yield normalized log objects as new lines are appended."""
        self._running = True
        delay = max(self._poll_ms, 1) / 1000

        f = None
        inode = 0
        pending = b""
        first_open = True

        try:
            while self._running:
                if f is None:
                    try:
                        f = open(self._path, "rb")
                    except OSError:
                        # Not there (yet) — a file that appears later is read from the start
                        first_open = False
                        await asyncio.sleep(delay)
                        continue
                    inode = os.fstat(f.fileno()).st_ino
                    pending = b""
                    if first_open and not self._from_beginning:
                        f.seek(0, os.SEEK_END)  # don't replay history on startup
                    first_open = False

                chunk = f.read(_READ_SIZE)
                if chunk:
                    pending += chunk
                    *lines, pending = pending.split(b"\n")
                    if len(pending) > _MAX_LINE_BYTES:
                        lines.append(pending)
                        pending = b""
                    for raw in lines:
                        line = raw.decode("utf-8", errors="replace").rstrip("\r")
                        if line.strip():
                            yield parse(line, source=self._source, adapter="file")
                    continue  # keep draining before sleeping

                # Caught up. Check for truncation, rotation or deletion.
                try:
                    st = os.stat(self._path)
                except OSError:
                    f.close()
                    f = None
                    await asyncio.sleep(delay)
                    continue

                rotated = bool(inode) and bool(st.st_ino) and st.st_ino != inode
                truncated = st.st_size < f.tell()
                if rotated or truncated:
                    f.close()
                    f = None  # reopen; first_open is False so we read the new file from the top
                    continue

                await asyncio.sleep(delay)
        finally:
            if f is not None:
                f.close()

    def stop(self) -> None:
        self._running = False
