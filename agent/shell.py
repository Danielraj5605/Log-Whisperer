"""Interactive shell — takes over terminal, runs commands, monitors logs."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from agent.procs import ainput, iter_lines, spawn, terminate

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
        console.print(Panel(Text.assemble(("Starting: ", "green"), cmd), border_style="dim"))

        # Ensure log directory exists
        self._log_file = None
        if self.log_path:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log_file = open(self.log_path, "a", encoding="utf-8", errors="replace")
            except OSError as exc:
                console.print(f"[yellow]Cannot write log file {escape(str(self.log_path))}: {escape(str(exc))}[/yellow]")

        try:
            self.process = await spawn(cmd)
            console.print(f"[dim]Process running: PID {self.process.pid} (Ctrl+C to stop it)[/dim]\n")

            # Read output line by line, display + tee
            async for line in iter_lines(self.process.stdout):
                console.print(Text(line), soft_wrap=True, highlight=False)
                if self._log_file:
                    self._log_file.write(line + "\n")
                    self._log_file.flush()

            await self.process.wait()
            console.print(f"\n[dim]Process exited with code {self.process.returncode}[/dim]\n")
        except (asyncio.CancelledError, KeyboardInterrupt):
            # Ctrl+C while a command runs stops the command, not the whole shell
            await self.stop_process()
            console.print("\n[dim]Command interrupted.[/dim]\n")
            task = asyncio.current_task()
            if task is not None and hasattr(task, "uncancel"):
                task.uncancel()
        except Exception as exc:
            console.print(f"\n[red]Error:[/red] {escape(str(exc))}\n")
        finally:
            self._close_log_file()
            self.running = False
            self.process = None

    async def stop_process(self) -> None:
        proc = self.process
        if proc is not None and proc.returncode is None:
            console.print(f"\n[dim]Stopping process {proc.pid}...[/dim]")
            await terminate(proc)

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
                f"[dim]Log file:[/dim] {escape(log_info)}\n"
                f"[dim]Type any command to run it.[/dim]\n"
                f"[dim]Output goes to terminal + log file.[/dim]\n"
                f"[dim]Type [green]exit[/green] to quit.[/dim]"
            ),
            border_style="cyan",
            padding=(1, 2),
        )
        console.print(banner)
        console.print()

        if self.log_path and not self.log_path.exists():
            console.print(f"[yellow]Log file '{escape(str(self.log_path))}' will be created on first command.[/yellow]\n")

        def read_command() -> str:
            console.print("[bold cyan]$[/bold cyan] ", end="")
            return sys.stdin.readline()

        while True:
            try:
                line = await ainput(read_command)
                if not line:  # EOF
                    break

                cmd = line.strip()
                if not cmd:
                    continue

                if cmd in ("exit", "quit", "q"):
                    break

                if cmd == "status":
                    self._print_status()
                    continue

                # Run command (blocks until done)
                await self.execute(cmd)

            except (EOFError, KeyboardInterrupt, asyncio.CancelledError):
                break

        await self.shutdown()

    def _print_status(self) -> None:
        if self.log_path:
            exists = "[green]exists[/green]" if self.log_path.exists() else "[red]not found[/red]"
            console.print(f"Log file: {escape(str(self.log_path))} ({exists})")
        proc = "[green]running[/green]" if (self.process and self.running) else "[dim]not running[/dim]"
        console.print(f"Process: {proc}\n")

    async def shutdown(self) -> None:
        """Kill subprocess and clean up."""
        await self.stop_process()
        self._close_log_file()
        console.print("[dim]Goodbye![/dim]")
