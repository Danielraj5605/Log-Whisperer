"""Interactive REPL — the main Log Whisperer shell with slash-commands."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any

from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from logwhisper_splash import show_splash

from agent.chat import ChatAgent, get_project_context
from agent.deps import check_service_deps
from agent.detect import detect_project_parts
from agent.pipeline import Monitor
from agent.procs import ainput, iter_lines, spawn, terminate
from agent.run import run_install
from agent.session import (
    clear_telegram_config,
    get_api_key_and_provider,
    get_telegram_config,
    mask_secret,
    save_global_api_key,
    save_telegram_config,
)
from buffer.parser import parse
from ingest.file_adapter import FileAdapter
from output.telegram_notification import TelegramNotifier
from output.terminal import console, print_service_log

logger = logging.getLogger("logwhisper")

# ANSI strip pattern
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

# Detect localhost URLs printed by dev servers (handles IPv4, IPv6, 0.0.0.0)
_URL_RE = re.compile(
    r"(https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::(?:1)?\])(?::\d{1,5})?(?:/\S*)?)",
    re.IGNORECASE,
)


# ─── Help text ────────────────────────────────────────────────────────────────

HELP_TEXT = """\
[bold]Available Commands:[/bold]

  [cyan]/help[/cyan]                      Show this help message
  [cyan]/run[/cyan]                      Start all detected services with monitoring
  [cyan]/run-frontend[/cyan]             Start only the frontend service
  [cyan]/run-backend[/cyan]              Start only the backend service
  [cyan]/stop[/cyan]                     Stop all running services
  [cyan]/watch \\[file][/cyan]             Watch a log file (e.g. /watch ./logs/app.log)
  [cyan]/chat \\[message][/cyan]           Ask the AI about errors (e.g. /chat why did the 401 happen?)
  [cyan]/telegram-setup[/cyan]           Run Telegram setup wizard
  [cyan]/telegram \\[test/status/clear][/cyan]  Manage Telegram alerts
  [cyan]/setup[/cyan]                    Run full setup wizard (API key + Telegram)
  [cyan]/exit[/cyan] or [cyan]Ctrl+C[/cyan]    Stop services and exit

  [dim]Tip: You can also just type a question without /chat — the AI will answer it.[/dim]
"""


# ─── REPL Session ─────────────────────────────────────────────────────────────

class REPLSession:
    """
    Interactive REPL for Log Whisperer.

    Manages:
    - Service processes (start/stop frontend, backend, or all)
    - Monitoring (trigger engine, agent worker)
    - Chat via ChatAgent
    - Telegram + Windows notifications
    """

    def __init__(self) -> None:
        self._running = False
        self._services_running = False

        # Project detection
        self._parts = detect_project_parts() or {}

        # API key and provider — don't prompt during init, let /setup or /chat handle it
        api_key, detected_provider = get_api_key_and_provider()
        self._api_key = api_key
        self._provider = detected_provider if detected_provider and detected_provider != "unknown" else "gemini"

        # Monitoring (created when services start)
        self._monitor: Monitor | None = None
        self._chat: ChatAgent | None = None

        # Service processes
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._log_file_handles: dict[str, Any] = {}

        # Background tasks
        self._all_tasks: list[asyncio.Task[Any]] = []

    # ── Public API ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main REPL loop — accepts input, routes commands, manages background tasks."""
        self._running = True
        self._print_banner()

        try:
            while self._running:
                try:
                    line = await ainput(lambda: console.input("[bold cyan]>[/bold cyan] "))
                except EOFError:
                    break
                cmd = line.strip()
                if not cmd:
                    continue
                await self._route(cmd)
        except (KeyboardInterrupt, asyncio.CancelledError):
            console.print()
        finally:
            # Always stop the services we started — never leave orphaned dev servers behind
            await self._cmd_stop(quiet=True)
            self._running = False

        self._print_goodbye()

    # ── Command Router ───────────────────────────────────────────────────────

    async def _route(self, cmd: str) -> None:
        """Route user input to the appropriate handler."""
        # No-slash chat — treat as a chat message
        if not cmd.startswith("/"):
            await self._cmd_chat(cmd)
            return

        name, _, args = cmd.partition(" ")
        name = name.lower()
        args = args.strip()

        if name == "/help":
            self._cmd_help()
        elif name == "/run":
            await self._cmd_run()
        elif name == "/run-frontend":
            await self._cmd_run_only("frontend", ("frontend",))
        elif name == "/run-backend":
            await self._cmd_run_only("backend", ("backend", "server"))
        elif name == "/stop":
            await self._cmd_stop()
        elif name == "/chat":
            await self._cmd_chat(args)
        elif name == "/watch":
            await self._cmd_watch(args)
        elif name == "/telegram-setup":
            await self._cmd_telegram_setup()
        elif name == "/telegram":
            await self._cmd_telegram(args)
        elif name == "/setup":
            await self._cmd_setup()
        elif name in ("/exit", "/quit", "/q"):
            self._running = False
        else:
            console.print(f"[yellow]Unknown command:[/yellow] {escape(cmd)}")
            console.print("[dim]Type /help for available commands[/dim]")

    # ── Command Handlers ────────────────────────────────────────────────────

    def _cmd_help(self) -> None:
        console.print(Panel(HELP_TEXT, title="Help", border_style="cyan", padding=(1, 2)))

    async def _cmd_run(self) -> None:
        """Start all detected services with full monitoring."""
        if self._services_running:
            console.print("[yellow]Services are already running.[/yellow]")
            self._print_service_status()
            return

        # Re-detect every time so we always pick up the latest detect.py output
        self._parts = detect_project_parts()
        if not self._parts:
            console.print("[yellow]No project detected. Use /run-frontend or /run-backend manually.[/yellow]")
            return

        console.print(f"[green]Starting {len(self._parts)} service(s)...[/green]")
        for name, info in self._parts.items():
            console.print(f"  [cyan]{escape(name)}[/cyan] — {escape(info['command'])}")
        console.print()

        if not await self._start_services(list(self._parts.keys())):
            return
        console.print("[green]Services started.[/green]")
        console.print("[dim]Use /stop to stop them, or /chat to ask about errors.[/dim]")

        # Print URLs for every service that has one — always shown, regardless of server output
        for name, info in self._parts.items():
            if name in self._processes and info.get("url"):
                console.print(
                    Panel(
                        f"[bold green]\U0001f310  {escape(name)}[/bold green]\n\n"
                        f"   [bold white underline]{escape(info['url'])}[/bold white underline]"
                        f"  [dim]← open in browser[/dim]",
                        border_style="green",
                        padding=(0, 2),
                        expand=False,
                    )
                )
        console.print()

    async def _cmd_run_only(self, label: str, name_hints: tuple[str, ...]) -> None:
        """Start only the services whose name contains one of the hints."""
        if self._services_running:
            console.print("[yellow]Services are already running.[/yellow]")
            return

        self._parts = detect_project_parts()
        selected = [k for k in self._parts if any(h in k.lower() for h in name_hints)]
        if not selected:
            console.print(f"[yellow]No {label} service detected.[/yellow]")
            return

        if await self._start_services(selected):
            console.print(f"[green]{label.capitalize()} started.[/green]\n")

    async def _cmd_stop(self, quiet: bool = False) -> None:
        """Stop all running services and cancel monitoring tasks."""
        if not self._processes and not self._all_tasks:
            if not quiet:
                console.print("[dim]No services running.[/dim]")
            self._services_running = False
            return

        if not quiet:
            console.print("[dim]Stopping services...[/dim]")

        # Cancel background tasks
        tasks, self._all_tasks = self._all_tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        # Kill process trees
        procs, self._processes = list(self._processes.values()), {}
        await asyncio.gather(*(terminate(p) for p in procs), return_exceptions=True)

        # Close log files
        for lf in self._log_file_handles.values():
            try:
                lf.close()
            except Exception:
                pass
        self._log_file_handles.clear()

        self._services_running = False
        if not quiet:
            console.print("[dim]All services stopped.[/dim]\n")

    async def _cmd_chat(self, message: str) -> None:
        """Send a message to the AI chat agent."""
        if not message:
            console.print("[dim]Usage: /chat <your question>  or just type your question directly[/dim]")
            return

        if not self._api_key:
            console.print("[red]No API key configured.[/red] Run [green]/setup[/green] first.")
            return

        console.print("[dim]Thinking...[/dim]")

        # ── Build service status block ─────────────────────────────────────
        context_lines: list[str] = []
        if self._processes:
            context_lines.append("[RUNNING SERVICES]")
            for name, proc in self._processes.items():
                status = "running" if proc.returncode is None else f"stopped (exit {proc.returncode})"
                cmd = self._parts.get(name, {}).get("command", "unknown")
                context_lines.append(f"  {name}: {status}  (cmd: {cmd})")
            context_lines.append("")
        else:
            context_lines.append("[NO SERVICES RUNNING — user has not run /run yet]")
            context_lines.append("")

        # ── Recent buffered log lines ─────────────────────────────────────
        log_lines: list[str] = []
        if self._monitor:
            for entry in self._monitor.buffer.recent(100):
                raw = entry.get("raw", "")
                if raw:
                    log_lines.append(f"[{entry.get('source', '')}] [{entry.get('level', 'INFO')}] {raw}")
        context_lines += log_lines or ["[No log lines captured yet]"]

        if self._chat is None or self._chat.api_key != self._api_key:
            self._chat = ChatAgent(api_key=self._api_key, provider=self._provider)

        try:
            response = await self._chat.ask(
                message,
                log_lines=context_lines,
                project_context=get_project_context(),
            )
        except Exception as exc:
            console.print(f"[red]Chat error:[/red] {escape(str(exc))}")
            return

        # Frame the chat reply with horizontal rules for quick visual parsing.
        console.print("[dim]" + ("-" * 60) + "[/dim]")
        console.print(Panel(Text(response), border_style="cyan", padding=(1, 2)))
        console.print("[dim]" + ("-" * 60) + "[/dim]")

    async def _cmd_watch(self, args: str) -> None:
        """Watch a log file, or stream the live service buffer if no file given."""
        if not args:
            # No file given — if services are running, replay buffer then stream live
            if self._processes and self._monitor:
                await self._stream_buffer(self._monitor)
            else:
                console.print("[dim]No services running. Usage:[/dim]")
                console.print("  /watch ./logs/app.log   -- tail a log file")
                console.print("  /run                    -- start services (then /watch shows live logs)")
            return

        # File path given — tail that file
        file_path = args
        if file_path.startswith("--file"):
            file_path = file_path[len("--file"):].strip()

        if not file_path:
            console.print("[dim]Usage: /watch ./logs/app.log[/dim]")
            return

        p = Path(file_path.strip("\"'"))
        if not p.is_file():
            console.print(f"[red]File not found:[/red] {escape(file_path)}")
            return

        console.print(f"[dim]Watching:[/dim] {escape(file_path)}")
        console.print("[dim]Press Ctrl+C to stop watching.[/dim]\n")

        adapter = FileAdapter(path=p, poll_ms=100)
        try:
            async for log_obj in adapter.stream():
                clean = _ANSI_RE.sub("", log_obj.get("raw", ""))
                if clean:
                    console.print(Text(clean), soft_wrap=True, highlight=False)
        except asyncio.CancelledError:
            self._swallow_cancel()
        finally:
            adapter.stop()
        console.print("[dim]Stopped watching.[/dim]\n")

    async def _stream_buffer(self, monitor: Monitor) -> None:
        """Replay the buffered service logs, then follow new lines until Ctrl+C."""
        console.print("[dim]Streaming live service logs (Ctrl+C to stop):[/dim]\n")
        seq = 0
        try:
            while True:
                entries, seq = monitor.buffer.since(seq)
                for entry in entries:
                    raw = _ANSI_RE.sub("", entry.get("raw", ""))
                    if raw:
                        console.print(
                            Text.assemble((f"[{entry.get('source', '')}] ", "dim"), raw),
                            soft_wrap=True,
                            highlight=False,
                        )
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            self._swallow_cancel()
        console.print("[dim]Stopped watching.[/dim]\n")

    @staticmethod
    def _swallow_cancel() -> None:
        """Ctrl+C cancels the REPL's task; for sub-commands it should only end the sub-command."""
        task = asyncio.current_task()
        if task is not None and hasattr(task, "uncancel"):
            task.uncancel()

    async def _cmd_telegram_setup(self) -> None:
        """Run Telegram setup wizard."""
        console.print("[dim]Running Telegram setup...[/dim]")
        from rich.prompt import Confirm

        existing = get_telegram_config()
        if existing["bot_token"] and existing["chat_id"]:
            console.print("[green]Telegram is already configured.[/green]")
            if not await ainput(lambda: Confirm.ask("Overwrite?", default=False)):
                return

        console.print("\n[cyan]Telegram Setup[/cyan]")
        console.print("Steps:")
        console.print("  1. Open Telegram → chat with @BotFather (https://t.me/BotFather)")
        console.print("  2. Send /newbot → copy your bot token")
        console.print("  3. Open your bot → send /start")
        console.print("  4. Forward a message to @userinfobot → copy your Chat ID\n")

        bot_token = await ainput(lambda: console.input("Bot token: ", password=True))
        chat_id = await ainput(lambda: console.input("Chat ID: "))

        if not bot_token.strip() or not chat_id.strip():
            console.print("[red]Token and chat ID cannot be empty.[/red]")
            return

        console.print("[dim]Sending test message...[/dim]")
        notifier = TelegramNotifier(bot_token=bot_token.strip(), chat_id=chat_id.strip())
        # Only persist credentials that actually work
        if await asyncio.to_thread(
            notifier.send_text, "✅ <b>Log Whisperer</b> — Telegram alerts configured!"
        ):
            save_telegram_config(bot_token, chat_id)
            console.print("[green]Telegram configured successfully![/green]\n")
        else:
            console.print("[red]Failed to send test message. Check your token and chat ID.[/red]")

    async def _cmd_telegram(self, action: str) -> None:
        """Manage Telegram alerts."""
        action = action.strip().lower() or "status"

        if action not in ("status", "test", "clear"):
            console.print(f"[yellow]Unknown Telegram action:[/yellow] {escape(action)}")
            console.print("Usage: /telegram \\[test|status|clear]")
            return

        if action == "clear":
            clear_telegram_config()
            console.print("[green]Telegram configuration cleared.[/green]")
            return

        cfg = get_telegram_config()
        if not cfg["bot_token"] or not cfg["chat_id"]:
            console.print("[yellow]Telegram not configured.[/yellow]")
            console.print("Run [green]/telegram-setup[/green] to configure.\n")
            return

        if action == "status":
            table = Table(box=None, show_header=False, padding=(0, 2))
            table.add_column(style="bold cyan")
            table.add_column(style="white")
            table.add_row("Bot token", mask_secret(cfg["bot_token"]))
            table.add_row("Chat ID", cfg["chat_id"])
            table.add_row("Status", "[green]Configured[/green]")
            console.print(table)
            return

        console.print("[dim]Sending test message...[/dim]")
        notifier = TelegramNotifier(bot_token=cfg["bot_token"], chat_id=cfg["chat_id"])
        if await asyncio.to_thread(
            notifier.send_text, "✅ <b>Log Whisperer</b> — Test message received!"
        ):
            console.print("[green]Test message sent![/green]")
        else:
            console.print("[red]Failed to send. Check your configuration.[/red]")

    async def _cmd_setup(self) -> None:
        """Run the full setup wizard."""
        from rich.prompt import Confirm

        console.print("[dim]Running setup wizard...[/dim]\n")

        existing_key, existing_provider = get_api_key_and_provider()
        if existing_key:
            console.print(f"[green]API key already configured.[/green] [dim]({escape(str(existing_provider))})[/dim]")
            console.print(f"  [dim]{mask_secret(existing_key)}[/dim]")
            if await ainput(lambda: Confirm.ask("Overwrite?", default=False)):
                await self._setup_api_key()
            else:
                console.print("[dim]Skipping API key setup.[/dim]")
        else:
            await self._setup_api_key()

        if await ainput(lambda: Confirm.ask("Set up Telegram alerts?", default=True)):
            await self._cmd_telegram_setup()

        console.print("[green]Setup complete![/green]\n")

    async def _setup_api_key(self) -> None:
        """Interactive API key setup for Gemini."""
        console.print("[cyan]API Key Setup[/cyan]\n")
        console.print("  Get your key at: [blue]https://aistudio.google.com/app/apikey[/blue]\n")

        key = (await ainput(lambda: console.input("Enter your Gemini API key: ", password=True))).strip()
        if not key:
            console.print("[red]API key cannot be empty.[/red]\n")
            return

        save_global_api_key(key, provider="gemini")
        self._api_key = key
        self._provider = "gemini"
        console.print("[green]API key saved![/green] (gemini-2.5-flash ready)\n")

    # ── Service Management ────────────────────────────────────────────────────

    async def _start_services(self, service_names: list[str]) -> bool:
        """Start services by name and begin monitoring. Returns True if any service started."""
        parts = {k: v for k, v in self._parts.items() if k in service_names}
        if not parts:
            return False

        if not self._api_key:
            console.print(
                "[yellow]No API key configured — services will run, but alerts can't be "
                "analyzed. Run /setup to add one.[/yellow]"
            )

        # Check and install dependencies BEFORE spawning services
        await self._check_and_install_deps(parts)

        monitor = Monitor(self._api_key or "")
        self._monitor = monitor

        started: dict[str, asyncio.subprocess.Process] = {}
        for name, info in parts.items():
            log_path = Path(info["log_path"])
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                lf = open(log_path, "a", encoding="utf-8", errors="replace")
            except OSError as exc:
                console.print(f"[red]Cannot open log file for {escape(name)}:[/red] {escape(str(exc))}")
                continue

            try:
                proc = await spawn(info["command"])
            except Exception as exc:
                lf.close()
                console.print(f"[red]Failed to start {escape(name)}:[/red] {escape(str(exc))}")
                continue

            self._log_file_handles[name] = lf
            self._processes[name] = proc
            started[name] = proc
            console.print(f"[green]Started {escape(name)}[/green] (PID {proc.pid})")

            url = info.get("url")
            if url:
                console.print(
                    Panel(
                        f"[bold green]\U0001f310  {escape(name)}[/bold green]\n\n"
                        f"   Open [bold white underline]{escape(url)}[/bold white underline]"
                        f"  [dim](server is starting...)[/dim]",
                        border_style="green",
                        padding=(0, 2),
                        expand=False,
                    )
                )

        console.print()
        if not started:
            return False

        self._services_running = True
        self._all_tasks.append(monitor.start_worker())
        for name, proc in started.items():
            self._all_tasks.append(
                asyncio.create_task(self._read_service_output(name, proc, self._log_file_handles[name]))
            )
        return True

    async def _read_service_output(
        self,
        name: str,
        proc: asyncio.subprocess.Process,
        log_file: Any,
    ) -> None:
        """Read subprocess stdout, print, tee to file, feed to trigger engine."""
        announced: set[str] = set()   # avoid printing same URL twice per service
        async for decoded in iter_lines(proc.stdout):
            clean = _ANSI_RE.sub("", decoded)
            if not clean.strip():
                continue

            print_service_log(name, clean)

            if log_file and not log_file.closed:
                log_file.write(clean + "\n")
                log_file.flush()

            # --- Live URL detection: catch the real URL/port the server chose ---
            url_match = _URL_RE.search(clean)
            if url_match:
                real_url = (
                    url_match.group(1).rstrip("/")
                    .replace("0.0.0.0", "localhost")
                    .replace("[::]", "localhost")
                    .replace("[::1]", "localhost")
                )
                if real_url not in announced:
                    announced.add(real_url)
                    console.print()
                    console.print(
                        Panel(
                            f"[bold green]\U0001f310  {escape(name)}[/bold green]  is ready\n\n"
                            f"   [bold white underline]{escape(real_url)}[/bold white underline]",
                            border_style="bright_green",
                            padding=(0, 2),
                            expand=False,
                        )
                    )
                    console.print()

            # Feed to trigger engine (evaluates BEFORE pushing to the buffer)
            if self._monitor:
                self._monitor.ingest(parse(clean, source=name, adapter="subprocess"))

        console.print(f"[dim]{escape(name)} exited (code {proc.returncode}).[/dim]")

    # ── Dependency Check ────────────────────────────────────────────────────

    async def _check_and_install_deps(self, parts: dict[str, dict]) -> None:
        """Check deps for all services, prompt before installing anything."""
        from rich.prompt import Confirm

        needing: list[tuple[str, Path]] = []
        for name, info in parts.items():
            service_path = Path(info["path"])
            if (await asyncio.to_thread(check_service_deps, name, service_path)).missing:
                needing.append((name, service_path))

        if not needing:
            return

        console.print()
        console.print(f"[yellow]⚠ Dependencies may be missing for {len(needing)} service(s).[/yellow]")
        for name, path in needing:
            console.print(f"  [cyan]{escape(name)}[/cyan]  ({escape(str(path))})")

        try:
            answer = await ainput(
                lambda: Confirm.ask("\nDependencies missing. Install now?", default=False)
            )
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Skipping install.[/dim]")
            return

        if answer:
            await asyncio.to_thread(run_install, needing, console)

    # ── UI Helpers ──────────────────────────────────────────────────────────

    def _print_banner(self) -> None:
        show_splash(
            console=console,
            api_key=self._api_key,
            provider=self._provider,
            cwd=os.getcwd(),
            parts=self._parts if self._parts else None,
        )

        console.print("  [cyan]/help[/cyan]   Show all commands")
        console.print("  [cyan]/run[/cyan]    Start monitoring all services")
        console.print("  [cyan]/chat[/cyan]   Ask the AI about your logs")
        console.print()

    def _print_service_status(self) -> None:
        if not self._processes:
            console.print("[dim]No services running.[/dim]")
            return
        for name, proc in self._processes.items():
            status = "[green]running[/green]" if proc.returncode is None else "[red]stopped[/red]"
            console.print(f"  [cyan]{escape(name)}[/cyan]  {status}  (PID {proc.pid})")
        console.print()

    def _print_goodbye(self) -> None:
        console.print("[dim]Goodbye![/dim]")
