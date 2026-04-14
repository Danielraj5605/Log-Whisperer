"""Interactive shell — takes over terminal, runs commands, monitors logs."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console()


class Shell:
    """Interactive shell: runs any command, tee-ing output to log file, while monitoring logs."""

    def __init__(self, log_path: str | Path | None = None) -> None:
        self.log_path = Path(log_path) if log_path else None
        self.process: asyncio.subprocess.Process | None = None
        self.running = False
        self._log_file: Any = None

    async def execute(self, cmd: str) -> None:
        """Execute a command, tee-ing output to log file + displaying to terminal."""
        self.running = True
        console.print(Panel(f"[green]Starting:[/green] {cmd}", border_style="dim"))

        # Ensure log directory exists
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = open(self.log_path, "a", encoding="utf-8", errors="replace")
        else:
            self._log_file = None

        try:
            self.process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            console.print(f"[dim]Process running: PID {self.process.pid} (type 'exit' to stop)[/dim]\n")

            # Read output line by line, display + tee
            while True:
                if self.process.stdout is None:
                    break
                line = await self.process.stdout.readline()
                if not line:
                    break

                decoded = line.decode("utf-8", errors="replace").rstrip("\r\n")
                print(decoded)

                if self._log_file:
                    self._log_file.write(decoded + "\n")
                    self._log_file.flush()

        except Exception as exc:
            console.print(f"\n[red]Error:[/red] {exc}\n")
        finally:
            self._close_log_file()
            self.running = False

        if self.process:
            await self.process.wait()
            code = self.process.returncode
            console.print(f"\n[dim]Process exited with code {code}[/dim]\n")
            self.process = None

    def _close_log_file(self) -> None:
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

    async def shell_loop(self, log_path: str | None = None) -> None:
        """Interactive shell loop — runs any command typed."""
        if log_path:
            self.log_path = Path(log_path)

        # Banner
        log_info = str(self.log_path) if self.log_path else "./logs/app.log"
        banner = Panel(
            Text.from_markup(
                f"[bold cyan]Log Whisperer[/bold cyan] v0.1.0\n\n"
                f"[dim]Log file:[/dim] {log_info}\n"
                f"[dim]Type any command to run it.[/dim]\n"
                f"[dim]Output goes to terminal + log file.[/dim]\n"
                f"[dim]Type [green]exit[/green] to quit."
            ),
            border_style="cyan",
            padding=(1, 2),
        )
        console.print(banner)
        console.print()

        if self.log_path and not self.log_path.exists():
            console.print(f"[yellow]Log file '{self.log_path}' will be created on first command.[/yellow]\n")

        while True:
            try:
                # Blocking input
                line = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
                if not line:
                    break

                cmd = line.strip()
                if not cmd:
                    continue

                if cmd in ("exit", "quit", "q"):
                    await self.shutdown()
                    break

                if cmd == "status":
                    self._print_status()
                    continue

                # Run command (blocks until done)
                await self.execute(cmd)

            except (EOFError, KeyboardInterrupt):
                await self.shutdown()
                break

    def _print_status(self) -> None:
        if self.log_path:
            p = Path(self.log_path)
            exists = "[green]exists[/green]" if p.exists() else "[red]not found[/red]"
            console.print(f"Log file: {self.log_path} ({exists})")
        proc = "[green]running[/green]" if (self.process and self.running) else "[dim]not running[/dim]"
        console.print(f"Process: {proc}\n")

    async def shutdown(self) -> None:
        """Kill subprocess and clean up."""
        if self.process and self.process.returncode is None:
            console.print(f"\n[dim]Stopping process {self.process.pid}...[/dim]")
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/PID", str(self.process.pid)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                else:
                    self.process.terminate()
                await asyncio.sleep(0.3)
                if self.process.returncode is None:
                    self.process.kill()
            except Exception:
                pass
        self._close_log_file()
        console.print("[dim]Goodbye![/dim]")
