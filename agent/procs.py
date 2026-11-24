"""Subprocess and terminal-input helpers shared by run / repl / shell."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
from typing import AsyncIterator, Callable, TypeVar

T = TypeVar("T")

_STREAM_LIMIT = 1024 * 1024  # max bytes per output line before the reader gives up on it


def child_environment() -> dict[str, str]:
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    for name in ("GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        env.pop(name, None)
    return env


async def spawn(command: str) -> asyncio.subprocess.Process:
    """Start a shell command with stdout+stderr merged into one pipe.

    The child gets its own process group (POSIX) so the whole tree can be stopped later,
    and PYTHONUNBUFFERED so Python services show their logs live instead of in 4KB bursts.
    """
    kwargs: dict = {}
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    return await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=child_environment(),
        limit=_STREAM_LIMIT,
        **kwargs,
    )


async def iter_lines(stream: asyncio.StreamReader | None) -> AsyncIterator[str]:
    """Yield decoded, non-empty lines from a subprocess stream until EOF."""
    if stream is None:
        return
    while True:
        try:
            raw = await stream.readline()
        except ValueError:
            continue  # a single over-long line was discarded; keep reading the rest
        except (ConnectionError, OSError):
            return
        if not raw:
            return
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line:
            yield line


def terminate_now(proc: asyncio.subprocess.Process) -> None:
    """Kill a process *and its children* (the shell wrapper alone is not enough)."""
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired, OSError):
        try:
            proc.terminate()
        except ProcessLookupError:
            pass


async def terminate(proc: asyncio.subprocess.Process, grace: float = 3.0) -> None:
    """Stop a process tree, escalating to SIGKILL on POSIX if it ignores SIGTERM."""
    terminate_now(proc)
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except asyncio.TimeoutError:
        try:
            if sys.platform == "win32":
                proc.kill()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


async def ainput(reader: Callable[[], T]) -> T:
    """Run a blocking read (e.g. `input`) without blocking the event loop.

    Uses a daemon thread rather than the default executor: a thread stuck waiting for
    keyboard input must not keep the interpreter alive after the program wants to exit.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = loop.create_future()

    def resolve(setter: Callable[[object], None], value: object) -> None:
        if not future.done():
            setter(value)

    def run() -> None:
        try:
            try:
                result = reader()
            except BaseException as exc:  # noqa: BLE001 - forwarded to the awaiting coroutine
                loop.call_soon_threadsafe(resolve, future.set_exception, exc)
            else:
                loop.call_soon_threadsafe(resolve, future.set_result, result)
        except RuntimeError:
            pass  # event loop already closed (program is exiting)

    threading.Thread(target=run, name="logwhisper-input", daemon=True).start()
    return await future
