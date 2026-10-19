"""Docker-container and stdin adapters — yield normalized log objects like FileAdapter."""

from __future__ import annotations

import asyncio
import sys
import threading
from typing import AsyncIterator

from buffer.parser import parse


class DockerAdapter:
    """Follow `docker logs -f` for a container (stdout and stderr merged)."""

    def __init__(self, container: str, tail: int = 0) -> None:
        self._container = container
        self._tail = tail
        self._proc: asyncio.subprocess.Process | None = None

    async def stream(self) -> AsyncIterator[dict]:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                "docker", "logs", "-f", "--tail", str(self._tail), self._container,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                limit=1024 * 1024,
            )
        except FileNotFoundError:
            raise RuntimeError("docker executable not found — is Docker installed and on PATH?") from None

        assert self._proc.stdout is not None
        try:
            while True:
                try:
                    raw = await self._proc.stdout.readline()
                except ValueError:
                    continue  # over-long line was discarded by the reader
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.strip():
                    yield parse(line, source=self._container, adapter="docker")
        finally:
            await self.stop()

        code = self._proc.returncode
        if code not in (None, 0):
            raise RuntimeError(f"`docker logs` for '{self._container}' exited with code {code}")

    async def stop(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass


class StdinAdapter:
    """Read log lines piped into the process (`tail -f app.log | logwhisper watch --stdin`)."""

    def __init__(self, source: str = "stdin") -> None:
        self._source = source

    async def stream(self) -> AsyncIterator[dict]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def reader() -> None:
            # Daemon thread: a blocked read must not keep the interpreter alive at exit
            try:
                for line in sys.stdin:
                    loop.call_soon_threadsafe(queue.put_nowait, line)
            except (ValueError, OSError):
                pass
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=reader, name="logwhisper-stdin", daemon=True).start()

        while True:
            line = await queue.get()
            if line is None:
                break
            line = line.rstrip("\r\n")
            if line.strip():
                yield parse(line, source=self._source, adapter="stdin")
