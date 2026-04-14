"""Interactive REPL — the main Log Whisperer shell with slash-commands."""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.columns import Columns
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from agent.agent import MiniMaxAgent
from agent.chat import ChatAgent, get_project_context
from agent.detect import detect_project_parts, detect_project_type, get_project_name
from agent.session import get_api_key, get_api_key_and_provider, get_fallback_key_and_provider, get_telegram_config
from buffer.parser import parse
from buffer.ring_buffer import RingBuffer
from ingest.file_adapter import FileAdapter
from output.terminal import print_alert, print_service_log
from output.telegram_notification import TelegramNotifier
from output.windows_notification import WindowsNotifier
from trigger.cooldown import CooldownTracker
from trigger.engine import TriggerEngine
from trigger.rules import (
    CriticalKeywordRule,
    HttpErrorRule,
    NewErrorTypeRule,
    RateSpikeRule,
)

console = Console()

# ANSI strip pattern (from run.py)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


# ─── Help text ────────────────────────────────────────────────────────────────

HELP_TEXT = """\
[bold]Available Commands:[/bold]

  [cyan]/help[/cyan]                      Show this help message
  [cyan]/run[/cyan]                      Start all detected services with monitoring
  [cyan]/run-frontend[/cyan]             Start only the frontend service
  [cyan]/run-backend[/cyan]              Start only the backend service
  [cyan]/stop[/cyan]                     Stop all running services
  [cyan]/watch [file][/cyan]             Watch a log file (e.g. /watch ./logs/app.log)
  [cyan]/chat [message][/cyan]           Ask the AI about errors (e.g. /chat why did the 401 happen?)
  [cyan]/telegram-setup[/cyan]           Run Telegram setup wizard
  [cyan]/telegram [test/status/clear][/cyan]  Manage Telegram alerts
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
    - Monitoring tasks (trigger engine, agent worker, file tailing)
    - Chat via ChatAgent
    - Telegram + Windows notifications
    """

    def __init__(self) -> None:
        self._running = False
        self._services_running = False

        # Project detection
        self._parts = detect_project_parts()
        if not self._parts:
            self._parts = {}

        # API key and provider — don't prompt during init, let /setup or /chat handle it
        api_key, detected_provider = get_api_key_and_provider()
        self._api_key = api_key
        self._provider = detected_provider if detected_provider != "unknown" else "minimax"

        # Monitoring components (created when services start)
        self._buffer: RingBuffer | None = None
        self._engine: TriggerEngine | None = None
        self._agent: MiniMaxAgent | None = None
        self._windows_notifier: WindowsNotifier | None = None
        self._telegram_notifier: TelegramNotifier | None = None

        # Service processes
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._log_files: dict[str, Any] = {}
        self._log_file_handles: dict[str, Any] = {}

        # Background tasks
        self._all_tasks: list[asyncio.Task[Any]] = []
        self._finding_count = [0]

    # ── Public API ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main REPL loop — accepts input, routes commands, manages background tasks."""
        self._running = True
        self._print_banner()

        while self._running:
            try:
                line = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: console.input("[bold cyan]>[/bold cyan] ")
                )
                if not line:
                    continue

                cmd = line.strip()
                if not cmd:
                    continue

                await self._route(cmd)

            except (EOFError, KeyboardInterrupt):
                await self._cmd_stop()
                self._running = False
                break

        self._print_goodbye()

    # ── Command Router ───────────────────────────────────────────────────────

    async def _route(self, cmd: str) -> None:
        """Route user input to the appropriate handler."""
        lower = cmd.lower()

        # No-slash chat — treat as a chat message
        if not lower.startswith("/"):
            await self._cmd_chat(cmd)
            return

        # Slash commands
        if lower == "/help":
            self._cmd_help()
        elif lower == "/run":
            await self._cmd_run()
        elif lower == "/run-frontend":
            await self._cmd_run_frontend()
        elif lower == "/run-backend":
            await self._cmd_run_backend()
        elif lower == "/stop":
            await self._cmd_stop()
        elif lower.startswith("/chat"):
            await self._cmd_chat(cmd[6:].strip())  # remove "/chat "
        elif lower.startswith("/watch"):
            await self._cmd_watch(cmd[7:].strip())
        elif lower == "/telegram-setup":
            await self._cmd_telegram_setup()
        elif lower.startswith("/telegram"):
            await self._cmd_telegram(cmd[10:].strip())
        elif lower == "/setup":
            await self._cmd_setup()
        elif lower in ("/exit", "/quit", "/q"):
            await self._cmd_stop()
            self._running = False
        else:
            console.print(f"[yellow]Unknown command:[/yellow] {cmd}")
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

        if not self._parts:
            console.print("[yellow]No project detected. Use /run-frontend or /run-backend manually.[/yellow]")
            return

        console.print(f"[green]Starting {len(self._parts)} service(s)...[/green]")
        for name, info in self._parts.items():
            console.print(f"  [cyan]{name}[/cyan] — {info['command']}")
        console.print()

        await self._start_all_services(list(self._parts.keys()))
        self._services_running = True
        console.print("[green]All services started.[/green]")
        console.print("[dim]Use /stop to stop them, or /chat to ask about errors.[/dim]\n")

    async def _cmd_run_frontend(self) -> None:
        """Start only the frontend service."""
        if self._services_running:
            console.print("[yellow]Services are already running.[/yellow]")
            return

        frontend_parts = {k: v for k, v in self._parts.items() if "frontend" in k.lower()}
        if not frontend_parts:
            console.print("[yellow]No frontend service detected.[/yellow]")
            return

        await self._start_all_services(list(frontend_parts.keys()))
        self._services_running = True
        console.print("[green]Frontend started.[/green]\n")

    async def _cmd_run_backend(self) -> None:
        """Start only the backend service."""
        if self._services_running:
            console.print("[yellow]Services are already running.[/yellow]")
            return

        backend_parts = {k: v for k, v in self._parts.items() if "backend" in k.lower() or "server" in k.lower()}
        if not backend_parts:
            console.print("[yellow]No backend service detected.[/yellow]")
            return

        await self._start_all_services(list(backend_parts.keys()))
        self._services_running = True
        console.print("[green]Backend started.[/green]\n")

    async def _cmd_stop(self) -> None:
        """Stop all running services and cancel monitoring tasks."""
        if not self._processes:
            console.print("[dim]No services running.[/dim]")
            self._services_running = False
            return

        console.print("[dim]Stopping services...[/dim]")

        # Cancel background tasks
        for task in self._all_tasks:
            task.cancel()
        self._all_tasks.clear()

        # Kill processes
        for name, proc in list(self._processes.items()):
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(proc.pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                else:
                    proc.terminate()
            except Exception:
                pass

        self._processes.clear()

        # Close log files
        for lf in self._log_file_handles.values():
            try:
                lf.close()
            except Exception:
                pass
        self._log_file_handles.clear()

        self._services_running = False
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

        # Gather recent log lines from running services
        log_lines: list[str] = []
        if self._buffer:
            for entry in self._buffer.window(seconds=120)[-50:]:
                log_lines.append(f"[{entry.get('level', 'INFO')}] {entry.get('raw', '')}")

        # Get fallback key for auto-failover
        fallback_key, fallback_provider = get_fallback_key_and_provider()

        try:
            agent = ChatAgent(api_key=self._api_key, provider=self._provider)
            response = await agent.ask(
                message,
                log_lines=log_lines,
                fallback_key=fallback_key,
                fallback_provider=fallback_provider,
            )
        except Exception as exc:
            console.print(f"[red]Chat error:[/red] {exc}")
            return

        # Frame the chat reply with horizontal rules for quick visual parsing (Claude-style).
        console.print("[dim]" + ("-" * 60) + "[/dim]")
        console.print(Panel(response, border_style="cyan", padding=(1, 2)))
        console.print("[dim]" + ("-" * 60) + "[/dim]")

    async def _cmd_watch(self, args: str) -> None:
        """Watch a log file — wraps the watch command."""
        if not args:
            console.print("[dim]Usage: /watch ./logs/app.log[/dim]")
            return

        # Parse simple --file argument
        file_path = args.strip()
        if file_path.startswith("--file"):
            file_path = file_path.replace("--file", "").strip()

        if not file_path:
            console.print("[dim]Usage: /watch ./logs/app.log[/dim]")
            return

        p = Path(file_path)
        if not p.exists():
            console.print(f"[red]File not found:[/red] {file_path}")
            return

        console.print(f"[dim]Watching:[/dim] {file_path}")
        console.print("[dim]Press Ctrl+C to stop watching.[/dim]\n")

        # Run a simple file tail
        adapter = FileAdapter(path=p, poll_ms=100)
        try:
            async for log_obj in adapter.stream():
                clean = _ANSI_RE.sub("", log_obj.get("raw", ""))
                if clean:
                    console.print(clean)
        except asyncio.CancelledError:
            pass
        console.print("[dim]Stopped watching.[/dim]\n")

    async def _cmd_telegram_setup(self) -> None:
        """Run Telegram setup wizard."""
        console.print("[dim]Running Telegram setup...[/dim]")
        # Import and call the setup function
        from agent.session import save_telegram_config, get_telegram_config, clear_telegram_config
        from rich.prompt import Confirm, Prompt
        from output.telegram_notification import TelegramNotifier

        existing = get_telegram_config()
        if existing["bot_token"] and existing["chat_id"]:
            console.print("[green]Telegram is already configured.[/green]")
            overwrite = Confirm.ask("Overwrite?", default=False)
            if not overwrite:
                return

        console.print("\n[cyan]Telegram Setup[/cyan]")
        console.print("Steps:")
        console.print("  1. Open Telegram → chat with [@BotFather](https://t.me/BotFather)")
        console.print("  2. Send /newbot → copy your bot token")
        console.print("  3. Open your bot → send /start")
        console.print("  4. Forward a message to @userinfobot → copy your Chat ID\n")

        bot_token = console.input("Bot token: ")
        chat_id = console.input("Chat ID: ")

        if not bot_token.strip() or not chat_id.strip():
            console.print("[red]Token and chat ID cannot be empty.[/red]")
            return

        save_telegram_config(bot_token, chat_id)
        console.print("[dim]Sending test message...[/dim]")

        notifier = TelegramNotifier(bot_token=bot_token.strip(), chat_id=chat_id.strip())
        if notifier.send_text("✅ <b>Log Whisperer</b> — Telegram alerts configured!"):
            console.print("[green]Telegram configured successfully![/green]\n")
        else:
            console.print("[red]Failed to send test message. Check your token and chat ID.[/red]")
            clear_telegram_config()

    async def _cmd_telegram(self, action: str) -> None:
        """Manage Telegram alerts."""
        action = action.strip().lower()
        from agent.session import get_telegram_config, clear_telegram_config
        from output.telegram_notification import TelegramNotifier

        cfg = get_telegram_config()

        if action == "clear":
            clear_telegram_config()
            console.print("[green]Telegram configuration cleared.[/green]")
            return

        if not cfg["bot_token"] or not cfg["chat_id"]:
            console.print("[yellow]Telegram not configured.[/yellow]")
            console.print("Run [green]/telegram-setup[/green] to configure.\n")
            return

        if action == "status" or not action:
            table = Table(box=None, show_header=False, padding=(0, 2))
            table.add_column(style="bold cyan")
            table.add_column(style="white")
            table.add_row("Bot token", f"{cfg['bot_token'][:10]}...{cfg['bot_token'][-4:]}")
            table.add_row("Chat ID", cfg["chat_id"])
            table.add_row("Status", "[green]Configured[/green]")
            console.print(table)
            return

        if action == "test":
            console.print("[dim]Sending test message...[/dim]")
            notifier = TelegramNotifier(bot_token=cfg["bot_token"], chat_id=cfg["chat_id"])
            if notifier.send_text("✅ <b>Log Whisperer</b> — Test message received!"):
                console.print("[green]Test message sent![/green]")
            else:
                console.print("[red]Failed to send. Check your configuration.[/red]")
            return

        console.print(f"[yellow]Unknown Telegram action:[/yellow] {action}")
        console.print("Usage: /telegram [test|status|clear]")

    async def _cmd_setup(self) -> None:
        """Run the full setup wizard."""
        from rich.prompt import Confirm

        console.print("[dim]Running setup wizard...[/dim]\n")

        # API key setup — show provider info
        existing_key, existing_provider = get_api_key_and_provider()
        if existing_key:
            provider_tag = f"[dim]({existing_provider})[/dim]"
            console.print(f"[green]API key already configured.[/green] {provider_tag}")
            console.print(f"  [dim]{existing_key[:12]}...{existing_key[-4:]}[/dim]")
            if not Confirm.ask("Overwrite?", default=False):
                console.print("[dim]Skipping API key setup.[/dim]")
            else:
                await self._setup_api_key()
        else:
            await self._setup_api_key()

        # Telegram setup
        if Confirm.ask("Set up Telegram alerts?", default=True):
            await self._cmd_telegram_setup()

        console.print("[green]Setup complete![/green]\n")

    async def _setup_api_key(self) -> None:
        """Interactive API key setup with provider selection."""
        from agent.session import save_global_api_key

        console.print("[cyan]API Key Setup[/cyan]\n")

        # Ask provider first
        provider_choice = console.input(
            "Which provider? ([bold]minimax[/bold] or [bold]gemini[/bold]): "
        ).strip().lower()

        if provider_choice not in ("minimax", "gemini"):
            console.print("[red]Invalid choice. Must be 'minimax' or 'gemini'. Skipping.[/red]\n")
            return

        # Show provider-specific instructions
        if provider_choice == "minimax":
            console.print("  Get your key at: [blue]https://www.minimaxi.com/[/blue]\n")
            key_label = "MiniMax API key"
        else:
            console.print("  Get your key at: [blue]https://aistudio.google.com/app/apikey[/blue]\n")
            key_label = "Gemini API key"

        key = console.input(f"Enter your {key_label}: ").strip()
        if not key:
            console.print("[red]API key cannot be empty.[/red]\n")
            return

        save_global_api_key(key.strip(), provider=provider_choice)
        self._api_key = key.strip()
        self._provider = provider_choice
        console.print(f"[green]API key saved![/green] ({provider_choice})\n")

    # ── Service Management ────────────────────────────────────────────────────

    async def _start_all_services(self, service_names: list[str]) -> None:
        """Start services by name and begin monitoring."""
        parts = {k: v for k, v in self._parts.items() if k in service_names}
        if not parts:
            return

        # Setup monitoring
        self._buffer = RingBuffer(maxlen=500)
        self._engine = TriggerEngine(
            rules=[
                NewErrorTypeRule(),
                RateSpikeRule(threshold=3, window_seconds=10),
                CriticalKeywordRule(),
                HttpErrorRule(),
            ],
            cooldowns=CooldownTracker(),
            agent_queue=asyncio.Queue(maxsize=50),
        )
        self._agent = MiniMaxAgent(api_key=self._api_key)
        self._windows_notifier = WindowsNotifier()

        tg_cfg = get_telegram_config()
        self._telegram_notifier = None
        if tg_cfg["bot_token"] and tg_cfg["chat_id"]:
            self._telegram_notifier = TelegramNotifier(
                bot_token=tg_cfg["bot_token"],
                chat_id=tg_cfg["chat_id"],
            )

        self._finding_count = [0]

        async def on_finding(finding: dict[str, Any]) -> None:
            self._finding_count[0] += 1
            source = finding.get("source", "unknown")
            if self._windows_notifier:
                try:
                    self._windows_notifier.notify(finding)
                except Exception:
                    pass
            if self._telegram_notifier:
                try:
                    self._telegram_notifier.notify(finding, project=get_project_name())
                except Exception:
                    pass
            print_alert(finding, suppressed_count=self._engine.suppression_count)

        # Create log file handles
        for name, info in parts.items():
            log_dir = Path(info["log_path"]).parent
            log_dir.mkdir(parents=True, exist_ok=True)
            lf = open(info["log_path"], "a", encoding="utf-8", errors="replace")
            self._log_file_handles[name] = lf

        # Spawn subprocesses
        for name, info in parts.items():
            lf = self._log_file_handles.get(name)
            try:
                proc = await asyncio.create_subprocess_shell(
                    info["command"],
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                self._processes[name] = proc
                console.print(f"[green]Started {name}[/green] (PID {proc.pid})")
            except Exception as exc:
                console.print(f"[red]Failed to start {name}:[/red] {exc}")

        console.print()

        # Start background tasks
        worker_task = asyncio.create_task(
            self._agent_worker(
                self._engine.agent_queue,  # type: ignore[union-attr]
                self._agent,
                on_finding,
            )
        )
        self._all_tasks.append(worker_task)

        for name, proc in self._processes.items():
            info = parts[name]
            lf = self._log_file_handles.get(name)

            # stdout reader task
            out_task = asyncio.create_task(
                self._read_service_output(name, proc, lf)
            )
            self._all_tasks.append(out_task)

            # log file monitor task
            mon_task = asyncio.create_task(
                self._monitor_service(name, info["log_path"])
            )
            self._all_tasks.append(mon_task)

    async def _read_service_output(
        self,
        name: str,
        proc: asyncio.subprocess.Process,
        log_file: Any,
    ) -> None:
        """Read subprocess stdout, print, tee to file, feed to trigger engine."""
        while True:
            if proc.stdout is None:
                break
            try:
                line = await proc.stdout.readline()
            except Exception:
                break
            if not line:
                break

            decoded = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not decoded:
                continue

            clean = _ANSI_RE.sub("", decoded)
            if not clean:
                continue

            print_service_log(name, clean)

            if log_file:
                log_file.write(clean + "\n")
                log_file.flush()

            # Feed to trigger engine
            if self._buffer and self._engine:
                log_obj = parse(clean, source=name, adapter="subprocess")
                self._engine.evaluate(log_obj, self._buffer)  # type: ignore[union-attr]
                self._buffer.push(log_obj)  # type: ignore[union-attr]

    async def _monitor_service(self, name: str, log_path: str) -> None:
        """Tail a service log file and feed to trigger engine."""
        adapter = FileAdapter(path=Path(log_path), poll_ms=100, source=name)
        while True:
            try:
                async for log_obj in adapter.stream():
                    if self._buffer and self._engine:
                        self._engine.evaluate(log_obj, self._buffer)
                        self._buffer.push(log_obj)
            except FileNotFoundError:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.5)

    async def _agent_worker(
        self,
        queue: asyncio.Queue[dict[str, Any]],
        agent: MiniMaxAgent,
        on_finding: Any,
    ) -> None:
        """Consume agent queue, call LLM, invoke on_finding."""
        while True:
            try:
                trigger_event = await asyncio.wait_for(queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                finding = await agent.analyze(trigger_event)
                await on_finding(finding)
            except Exception as exc:
                import logging
                logging.getLogger("logwhisper").error("Agent worker error: %s", exc)
            finally:
                queue.task_done()

    # ── UI Helpers ──────────────────────────────────────────────────────────

    def _print_banner(self) -> None:
        import os

        avatar_lines = [
            "   _________  ",
            "  |         | ",
            "  |  O   O  | ",
            "  |    o    | ",
            "  |   ___   | ",
            "  |_________| ",
        ]

        cwd = os.getcwd()

        if self._api_key:
            key_status = "[green]API Key Active[/green]"
            key_display = f"{self._api_key[:12]}... [{self._provider}]"
        else:
            key_status = "[red]Not configured[/red]"
            key_display = "Run /setup to configure"

        avatar_panel = Panel(
            Text("\n".join(avatar_lines), style="bold cyan"),
            border_style="cyan",
            padding=(1, 1),
        )

        status_lines = []
        status_lines.append("[bold white]Welcome back![/bold white]")
        status_lines.append("")
        status_lines.append("LogWhisper v0.1.0")
        status_lines.append(key_status)
        if self._api_key:
            status_lines.append(f"[dim]{key_display}[/dim]")
        status_lines.append("")
        status_lines.append(f"[dim]{cwd}[/dim]")

        status_panel = Panel(
            Text("\n".join(status_lines)),
            border_style="cyan",
            padding=(1, 2),
        )

        from rich.columns import Columns
        banner_content = Columns([avatar_panel, status_panel], expand=True)

        console.print()
        console.print(banner_content)
        console.print()

        if self._parts:
            console.print("[green]Project:[/green] " + str(len(self._parts)) + " service(s) detected")
            for name, info in self._parts.items():
                console.print("  [cyan]" + name + "[/cyan]  " + info["command"])
            console.print()
        else:
            console.print("[yellow]Project:[/yellow] No project detected")
            console.print("  Use [cyan]/run-frontend[/cyan] or [cyan]/run-backend[/cyan] to start manually.\n")

        console.print("[bold]Usage[/bold]\n")
        console.print("  [cyan]/help[/cyan]     Show all commands")
        console.print("  [cyan]/run[/cyan]     Start monitoring all services")
        console.print("  [cyan]/chat <msg>[/cyan]  Ask about logs\n")

    def _print_service_status(self) -> None:
        if not self._processes:
            console.print("[dim]No services running.[/dim]")
            return
        for name, proc in self._processes.items():
            alive = proc.returncode is None
            status = "[green]running[/green]" if alive else "[red]stopped[/red]"
            console.print(f"  [cyan]{name}[/cyan]  {status}  (PID {proc.pid})")
        console.print()

    def _print_goodbye(self) -> None:
        console.print("[dim]Goodbye![/dim]")
