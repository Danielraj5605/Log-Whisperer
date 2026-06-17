"""File tail adapter — watches a log file and yields new lines as they are appended."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import AsyncIterator

from buffer.parser import parse


class FileAdapter:
    """Tail a file, detecting log rotation, yielding normalized log objects."""

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
        self._pos = 0
        self._inode: int | None = None
        self._running = False
        self._from_beginning = from_beginning

    def _get_inode(self) -> int | None:
        try:
            stat = self._path.stat()
            return stat.st_ino
        except OSError:
            return None

    def _open_at_inode(self) -> tuple[int, int]:
        """Open file, detect rotation, return (inode, position)."""
        inode = self._get_inode()
        if inode is None:
            raise FileNotFoundError(f"File not found: {self._path}")

        # Rotation detected: file was replaced
        if self._inode is not None and inode != self._inode:
            self._pos = 0

        self._inode = inode

        f = open(self._path, "r", encoding="utf-8", errors="replace")
        if self._pos == 0 and not self._from_beginning:
            # Seek to end on first open (don't replay old logs)
            f.seek(0, os.SEEK_END)
            self._pos = f.tell()
        else:
            f.seek(self._pos)
        return inode, f

    async def stream(self) -> AsyncIterator[dict]:
        """Yield normalized log objects as new lines are appended."""
        self._running = True

        while self._running:
            try:
                inode, f = self._open_at_inode()
            except FileNotFoundError:
                await asyncio.sleep(self._poll_ms / 1000)
                continue

            try:
                for line in f:
                    line = line.rstrip("\n\r")
                    if not line:
                        continue
                    self._pos = f.tell()
                    yield parse(line, source=self._source, adapter="file")
            finally:
                f.close()

            await asyncio.sleep(self._poll_ms / 1000)

    def stop(self) -> None:
        self._running = False
