"""Log Whisperer CLI — main entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape

log = logging.getLogger("logwhisper")

app = typer.Typer(add_completion=False)

BUFFER_MAXLEN = 500
ALL_COMMANDS = ("run", "shell", "watch", "analyze", "setup", "telegram", "chat", "project")


def _ensure_utf8_output() -> None:
    """Make stdout/stderr UTF-8 and never raise on unencodable characters.

    The UI uses emoji and box/block characters; on Windows, redirected or legacy-console
    output defaults to cp1252 and Rich would crash with UnicodeEncodeError.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _setup_logging() -> None:
    _ensure_utf8_output()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx logs every request URL at INFO — noise, and can contain secrets
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _resolve_api_key(cli_key: str | None, local: bool = False) -> str:
    """CLI flag > GEMINI_API_KEY > saved credentials > project config. Exits if none."""
    from agent.session import get_api_key

    if local:
        return ""
    key = (cli_key or "").strip() or get_api_key() or ""
    if not key:
        log.error("No API key found. Set GEMINI_API_KEY, pass --local for rules-only monitoring, or run `logwhisper setup`.")
        raise typer.Exit(1)
    return key


@app.callback(invoke_without_command=True)
def main_callback(ctx: typer.Context) -> None:
    """Called when no subcommand is given — launches the interactive REPL."""
    _setup_logging()
    if ctx.invoked_subcommand is None:
        from agent.repl import REPLSession

        try:
            asyncio.run(REPLSession().run())
        except KeyboardInterrupt:
            pass


@app.command()
def run(
    local: bool = typer.Option(False, "--local", help="Analyze with local rules and disable remote delivery"),
    cwd: Path | None = typer.Option(None, "--cwd", help="Project root directory"),
    auto_install: bool = typer.Option(
        False, "--auto-install", help="Automatically install missing dependencies without prompting"
    ),
    skip_deps_check: bool = typer.Option(
        False, "--skip-deps-check", help="Skip dependency check before starting services"
    ),
) -> None:
    """Auto-detect project, start dev server, and monitor logs (legacy)."""
    from agent.run import run as run_session
    from config import load as load_config

    if cwd:
        if not cwd.is_dir():
            typer.echo(f"Directory not found: {cwd}", err=True)
            raise typer.Exit(1)
        os.chdir(cwd)

    deps_cfg = load_config().get("deps", {})
    # CLI flags override config; config defaults are False
    final_auto_install = auto_install or bool(deps_cfg.get("auto_install", False))
    final_skip_check = skip_deps_check or bool(deps_cfg.get("skip_check", False))

    try:
        asyncio.run(run_session(auto_install=final_auto_install, skip_deps_check=final_skip_check, local=local))
    except KeyboardInterrupt:
        pass


@app.command()
def project(
    paths: list[Path] = typer.Argument(..., help="Project root directories to watch"),
    parallel: bool = typer.Option(False, "--parallel", "-p", help="Launch all windows and return immediately"),
) -> None:
    """
    Launch multiple independent Log Whisperer instances, one per project.

    Each project runs in its own terminal window with full Log Whisperer
    functionality: auto-detected services, log monitoring, and AI alerts.

    Example:
        logwhisper project ./backend ./frontend
        logwhisper project /path/to/project1 /path/to/project2 --parallel
    """
    console = Console()
    processes: list[tuple[str, subprocess.Popen]] = []

    for project_path in paths:
        abs_path = project_path.resolve()
        if not abs_path.is_dir():
            console.print(f"[red]Not a directory: {escape(str(abs_path))}[/red]")
            continue

        project_name = abs_path.name
        cmd = [sys.executable, "-m", "agent", "run", "--cwd", str(abs_path)]
        cmd_str = shlex.join(cmd)

        try:
            if sys.platform == "win32":
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(abs_path),
                    creationflags=subprocess.CREATE_NEW_CONSOLE,  # type: ignore[attr-defined]
                )
            elif sys.platform == "darwin":
                # macOS: open a new Terminal window (escape for AppleScript string literal)
                script_cmd = cmd_str.replace("\\", "\\\\").replace('"', '\\"')
                proc = subprocess.Popen(
                    ["osascript", "-e", f'tell app "Terminal" to do script "{script_cmd}"'],
                    cwd=str(abs_path),
                )
            else:
                proc = _launch_linux_terminal(cmd_str, abs_path, project_name)
        except (OSError, RuntimeError) as exc:
            console.print(f"[red]Could not launch {escape(project_name)}: {escape(str(exc))}[/red]")
            continue

        processes.append((project_name, proc))
        console.print(f"[green]Started {escape(project_name)}[/green] (PID {proc.pid})")

    if not processes:
        console.print("[yellow]No projects started.[/yellow]")
        raise typer.Exit(1)

    if parallel:
        console.print(f"[green]{len(processes)} project(s) launched.[/green]")
        return

    console.print("\n[dim]Waiting for all projects... Ctrl+C to stop waiting.[/dim]")
    try:
        for _, proc in processes:
            proc.wait()
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped waiting (project windows keep running).[/dim]")


def _launch_linux_terminal(cmd_str: str, cwd: Path, project_name: str) -> subprocess.Popen:
    """Open the command in a new terminal window: gnome-terminal -> xterm -> tmux."""
    attempts = [
        ["gnome-terminal", "--", "bash", "-c", f"{cmd_str}; exec bash"],
        ["xterm", "-hold", "-e", cmd_str],
        ["tmux", "new-window", "-n", f"lw-{project_name}", cmd_str],
    ]
    for argv in attempts:
        try:
            return subprocess.Popen(argv, cwd=str(cwd))
        except FileNotFoundError:
            continue
    raise RuntimeError("no terminal emulator found (tried gnome-terminal, xterm, tmux)")


@app.command()
def shell(
    local: bool = typer.Option(False, "--local", help="Analyze with local rules and disable remote delivery"),
    log_file: str | None = typer.Option(
        None, "--log-file", help="Log file path (overrides config default)"
    ),
    model: str | None = typer.Option(None, "--model", help="Gemini model name"),
    api_key: str | None = typer.Option(None, "--api-key", help="Gemini API key (or set GEMINI_API_KEY env)"),
    no_notification: bool = typer.Option(
        False, "--no-notification", help="Disable Windows toast notifications"
    ),
) -> None:
    """Interactive mode: run commands and monitor logs in the same terminal."""
    from agent.pipeline import Monitor
    from agent.shell import Shell
    from config import load as load_config
    from buffer.parser import parse

    key = _resolve_api_key(api_key, local)
    cfg = load_config()
    default_files = cfg.get("watch", {}).get("default_files") or []
    log_path = log_file or (default_files[0] if default_files else "./logs/app.log")

    async def main() -> None:
        monitor = Monitor(key, cfg=cfg, model=model, windows_notifications=not no_notification)
        monitor.start_worker()
        path = Path(log_path)

        def consume_line(line: str) -> None:
            monitor.ingest(parse(line, source=path.name, adapter="subprocess"))
            monitor.capture(path, line)

        try:
            await Shell(log_path=path, on_line=consume_line).shell_loop()
        finally:
            await monitor.finish()
        if monitor.degraded:
            raise typer.Exit(1)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    log.info("Shutdown complete.")


@app.command()
def watch(
    local: bool = typer.Option(False, "--local", help="Analyze with local rules and disable remote delivery"),
    file: list[Path] = typer.Option(
        None, "--file", "-f", help="Log file(s) to tail"
    ),
    docker: list[str] = typer.Option(
        None, "--docker", "-d", help="Docker container name(s) to watch"
    ),
    stdin: bool = typer.Option(
        False, "--stdin", help="Read log lines piped into logwhisper"
    ),
    poll_ms: int = typer.Option(
        100, "--poll", min=1, help="File polling interval in milliseconds"
    ),
    buffer_size: int | None = typer.Option(
        None, "--buffer", "-b", min=1, help="Ring buffer max lines"
    ),
    model: str | None = typer.Option(
        None, "--model", help="Gemini model name (default: from config, else gemini-2.5-flash)"
    ),
    api_key: str = typer.Option(
        None, "--api-key", help="Gemini API key (or set GEMINI_API_KEY env)"
    ),
    no_notification: bool = typer.Option(
        False, "--no-notification", help="Disable Windows toast notifications"
    ),
    no_telegram: bool = typer.Option(
        False, "--no-telegram", help="Disable Telegram alerts"
    ),
) -> None:
    """Watch log file(s), Docker containers or piped stdin in real-time and fire AI-analyzed alerts."""
    from agent.pipeline import Monitor
    from config import load as load_config
    from ingest.file_adapter import FileAdapter
    from ingest.stream_adapters import DockerAdapter, StdinAdapter

    key = _resolve_api_key(api_key, local)
    cfg = load_config()

    files = list(file or [])
    containers = list(docker or [])
    if not files and not containers and not stdin:
        default_paths = cfg.get("watch", {}).get("default_files") or []
        files = [Path(p) for p in default_paths]
    if not files and not containers and not stdin:
        log.error("Provide at least one source: --file, --docker or --stdin")
        raise typer.Exit(1)

    missing = [f for f in files if not f.exists()]
    for f in missing:
        log.warning("%s does not exist yet — will start tailing when it appears", f)

    files = list(dict.fromkeys(f.resolve() for f in files))
    containers = list(dict.fromkeys(containers))
    names = [f.name for f in files]
    reserved = {"stdin", *(f"docker:{name}" for name in containers)}
    sources = [str(f) if names.count(f.name) > 1 or f.name in reserved
               else f.name for f in files]
    sources += [f"docker:{name}" for name in containers]
    if stdin:
        sources.append("stdin")

    async def run_live() -> None:
        monitor = Monitor(
            key,
            cfg=cfg,
            model=model,
            buffer_size=buffer_size,
            windows_notifications=not no_notification,
            telegram=not no_telegram,
        )
        log.info("Watching %s", ", ".join(sources))

        adapters: list[Any] = [
            FileAdapter(path=f, poll_ms=poll_ms, source=name)
            for f, name in zip(files, sources)
        ]
        adapters += [DockerAdapter(c) for c in containers]
        if stdin:
            adapters.append(StdinAdapter())

        monitor.start_worker()
        readers = [asyncio.create_task(monitor.consume(a)) for a in adapters]
        try:
            # File tails run forever; docker/stdin sources end on their own. If every source
            # has ended there is nothing left to watch, so drain pending alerts and exit.
            await asyncio.gather(*readers)
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            await monitor.finish()
        if monitor.degraded:
            raise typer.Exit(1)

    try:
        asyncio.run(run_live())
    except KeyboardInterrupt:
        log.info("Interrupted by user")


@app.command()
def analyze(
    local: bool = typer.Option(False, "--local", help="Analyze with local rules and disable remote delivery"),
    file: Path = typer.Option(..., "--file", help="Log file to analyze"),
    since: str = typer.Option(None, "--since", help="Only lines at/after this time (ISO 8601)"),
    until: str = typer.Option(None, "--until", help="Only lines at/before this time (ISO 8601)"),
    max_alerts: int = typer.Option(10, "--max-alerts", min=0, help="Maximum number of anomalies to analyze with the LLM"),
    model: str = typer.Option(None, "--model", help="Gemini model name"),
    api_key: str = typer.Option(None, "--api-key", help="Gemini API key (or set GEMINI_API_KEY env)"),
) -> None:
    """Analyze an existing log file in batch mode (replays it through the trigger rules)."""
    from agent.pipeline import Monitor
    from buffer.parser import parse
    from config import load as load_config
    from trigger.cooldown import CooldownTracker

    def parse_bound(value: str | None, name: str) -> float | None:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            typer.echo(f"Invalid --{name} value {value!r}: use ISO 8601, e.g. 2026-04-08T10:00:00", err=True)
            raise typer.Exit(1)
        if dt.tzinfo is None:
            dt = dt.astimezone()  # naive input = local time
        return dt.astimezone(timezone.utc).timestamp()

    since_ts = parse_bound(since, "since")
    until_ts = parse_bound(until, "until")
    if since_ts is not None and until_ts is not None and since_ts > until_ts:
        raise typer.BadParameter("--since must be at or before --until")

    if not file.is_file():
        typer.echo(f"File not found: {file}", err=True)
        raise typer.Exit(1)
    key = _resolve_api_key(api_key, local)

    async def main() -> None:
        monitor = Monitor(
            key,
            cfg=load_config(),
            model=model,
            windows_notifications=False,
            telegram=False,
        )
        # Replay on the log's own timeline instead of wall-clock time
        now = [0.0]
        monitor.buffer.clock = lambda: now[0]
        monitor.engine.cooldowns = CooldownTracker(clock=lambda: now[0])
        events = []

        total = 0
        with open(file, encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.rstrip("\r\n")
                if not line.strip():
                    continue
                log_obj = parse(line, source=file.name, adapter="file")
                ts = log_obj["epoch"]
                if (since_ts is not None and ts < since_ts) or (until_ts is not None and ts > until_ts):
                    continue
                now[0] = ts
                monitor.ingest(log_obj, live=False)
                total += 1
                if not monitor.queue.empty():
                    event = monitor.queue.get_nowait()
                    if len(events) < max_alerts:
                        events.append(event)
                    monitor.queue.task_done()

        typer.echo(f"Scanned {total} lines — {monitor.engine.accepted} anomaly trigger(s), {monitor.suppression_count} suppressed.")
        if not events:
            return
        if monitor.engine.accepted > max_alerts:
            typer.echo(f"Analyzing the first {max_alerts} (raise --max-alerts to see more).")

        for event in events:
            await monitor.handle_finding(await monitor.agent.analyze(event))
        if monitor.delivery_failures:
            raise typer.Exit(1)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


@app.command()
def setup(
    skip_telegram: bool = typer.Option(
        False, "--skip-telegram", help="Skip Telegram setup"
    ),
    skip_api_key: bool = typer.Option(
        False, "--skip-api-key", help="Skip API key setup"
    ),
) -> None:
    """
    Guided first-time setup for Log Whisperer.

    Configures your API key and Telegram alerts interactively.
    Run this once after installing Log Whisperer.
    """
    # ── Python version guard ───────────────────────────────────────────
    if sys.version_info < (3, 11):
        typer.echo(
            f"\n[ERROR] Log Whisperer requires Python 3.11 or newer.\n"
            f"  You are running Python {sys.version_info.major}.{sys.version_info.minor}.\n"
            f"  Download the latest Python from: https://www.python.org/downloads/\n"
        )
        raise typer.Exit(1)

    from rich.panel import Panel
    from rich.prompt import Confirm
    from agent.session import get_api_key, mask_secret

    console = Console()

    console.print(
        Panel(
            "[bold cyan]Log Whisperer Setup[/bold cyan]\n"
            "[dim]Let's get you up and running![/dim]",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    console.print()

    # ── Step 1: API Key ────────────────────────────────────────────────
    if not skip_api_key:
        existing_key = get_api_key()
        if existing_key:
            console.print("[green]API key already configured.[/green]")
            console.print(f"  {mask_secret(existing_key)}")
            if Confirm.ask("Overwrite it?"):
                _setup_api_key_interactive(console)
            else:
                console.print("[dim]Skipping API key setup.[/dim]\n")
        else:
            _setup_api_key_interactive(console)

    # ── Step 2: Telegram ───────────────────────────────────────────────
    if not skip_telegram:
        _setup_telegram_interactive(console)

    # ── Step 3: Test ───────────────────────────────────────────────────
    console.print()
    _run_setup_tests(console)


def _setup_api_key_interactive(console: Console) -> None:
    """Prompt for and save a Gemini API key."""
    from rich.prompt import Prompt
    from agent.session import save_global_api_key

    console.print("[cyan]Step 1:[/cyan] API Key Setup\n")
    console.print("  Get your key at: [blue]https://aistudio.google.com/app/apikey[/blue]\n")

    key = Prompt.ask("Enter your Gemini API key", password=True).strip()
    if not key:
        console.print("[red]API key cannot be empty. Skipping.[/red]\n")
        return

    save_global_api_key(key, provider="gemini")
    console.print("[green]API key saved![/green] (gemini-2.5-flash ready)\n")


def _setup_telegram_interactive(console: Console) -> None:
    """Prompt for and save Telegram bot credentials."""
    from rich.prompt import Confirm, Prompt
    from agent.session import clear_telegram_config, get_telegram_config, save_telegram_config
    from output.telegram_notification import TelegramNotifier

    console.print("[cyan]Step 2:[/cyan] Telegram Alerts (optional)")
    console.print(
        "  Enable alerts sent directly to your Telegram chat.\n"
    )

    if not Confirm.ask("Do you want to set up Telegram alerts?"):
        # If already configured, offer to clear it
        existing = get_telegram_config()
        if existing["bot_token"] and existing["chat_id"]:
            if Confirm.ask("Clear existing Telegram configuration?"):
                clear_telegram_config()
                console.print("[dim]Telegram config cleared.[/dim]")
        else:
            console.print("[dim]Skipping Telegram setup.[/dim]")
        console.print()
        return

    console.print("\n[bold]How to get your Telegram credentials:[/bold]")
    console.print("  1. Open Telegram → chat with @BotFather (https://t.me/BotFather)")
    console.print("  2. Send [bold]/newbot[/bold], follow the prompts → copy the bot token")
    console.print("  3. Open your bot's chat → send [bold]/start[/bold]")
    console.print("  4. Forward any message from the bot to @userinfobot → copy your Chat ID\n")

    bot_token = Prompt.ask("Bot token (e.g. 123456789:ABCdef...)", password=True).strip()
    chat_id = Prompt.ask("Chat ID (e.g. 123456789)").strip()

    if not bot_token or not chat_id:
        console.print("[red]Token and chat ID cannot be empty. Skipping Telegram.[/red]\n")
        return

    console.print("\n[dim]Sending test message...[/dim]")
    notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)
    # Only persist credentials that actually work
    if notifier.send_text("✅ <b>Log Whisperer</b> — Telegram alerts are configured and working!"):
        save_telegram_config(bot_token, chat_id)
        console.print("[green]Telegram configured! Test message sent.[/green]\n")
    else:
        console.print("[red]Failed to send test message. Check your token and chat ID.[/red]")
        console.print("[dim]Telegram setup skipped.[/dim]\n")


def _run_setup_tests(console: Console) -> None:
    """Run validation tests after setup and print results."""
    from agent.session import get_api_key, get_telegram_config
    from rich.panel import Panel

    console.print("[cyan]Step 3:[/cyan] Validation")

    if get_api_key():
        console.print("  [green]✓[/green] API key configured")
    else:
        console.print("  [red]✗[/red] No API key — alerts won't work")
        console.print("    Run [green]logwhisper setup --skip-telegram[/green] to add it later\n")
        return

    tg_config = get_telegram_config()
    if tg_config["bot_token"] and tg_config["chat_id"]:
        console.print("  [green]✓[/green] Telegram configured")
    else:
        console.print("  [dim]  – Telegram not configured (optional)[/dim]\n")

    console.print()
    console.print(
        Panel(
            "[bold green]Setup complete![/bold green]\n"
            "Run [cyan]logwhisper run[/cyan] to start monitoring your logs.\n"
            "Run [cyan]logwhisper watch --file ./your.log[/cyan] to watch a specific file.",
            border_style="green",
            padding=(1, 2),
        )
    )
    console.print()


@app.command()
def telegram(
    action: str = typer.Argument(
        "status",
        help="Action: status, test, or clear",
    ),
) -> None:
    """Manage Telegram alerts: status, test, or clear."""
    from rich.table import Table
    from agent.session import clear_telegram_config, get_telegram_config, mask_secret
    from output.telegram_notification import TelegramNotifier

    console = Console()

    if action not in ("status", "test", "clear"):
        console.print(f"[red]Unknown action: {escape(action)}[/red]")
        console.print("Usage: logwhisper telegram \\[status|test|clear]")
        raise typer.Exit(1)

    if action == "clear":
        clear_telegram_config()
        console.print("[green]Telegram configuration cleared.[/green]")
        return

    config = get_telegram_config()
    if not config["bot_token"] or not config["chat_id"]:
        console.print("[yellow]Telegram not configured.[/yellow]")
        console.print("Run [green]logwhisper setup[/green] to configure.\n")
        raise typer.Exit(1)

    if action == "status":
        table = Table(box=None, show_header=False, padding=(0, 2))
        table.add_column(style="bold cyan")
        table.add_column(style="white")
        table.add_row("Bot token", mask_secret(config["bot_token"]))
        table.add_row("Chat ID", config["chat_id"])
        table.add_row("Status", "[green]Configured[/green]")
        console.print(table)
        return

    console.print("Sending test message...")
    notifier = TelegramNotifier(bot_token=config["bot_token"], chat_id=config["chat_id"])
    if notifier.send_text("✅ <b>Log Whisperer</b> — Test message received!"):
        console.print("[green]Test message sent![/green]")
    else:
        console.print("[red]Failed to send. Check your configuration.[/red]")
        raise typer.Exit(1)


@app.command()
def chat(
    preview: bool = typer.Option(False, "--preview", help="Print sanitized context locally and exit"),
    error: str | None = typer.Option(
        None, "--error", "-e", help="Single-shot: analyze this error or log excerpt"
    ),
    file: Path | None = typer.Option(
        None, "--file", "-f", help="Load recent log lines from a file for context"
    ),
    provider: str = typer.Option(
        "gemini", "--provider", "-p",
        help="LLM provider (only gemini is supported)",
    ),
    model: str | None = typer.Option(
        None, "--model", "-m", help="Model name (defaults to provider's default)",
    ),
) -> None:
    """
    Interactive chat with Log Whisperer — paste errors or ask questions about your project.

    Run without arguments for interactive REPL mode, or use --error for single-shot analysis.

    The API key comes from GEMINI_API_KEY or `logwhisper setup`.
    """
    from agent.chat import ChatAgent, get_project_context
    from agent.session import get_api_key

    console = Console()

    if provider.lower() != "gemini":
        console.print(f"[red]Unsupported provider '{escape(provider)}'.[/red] Only gemini is supported.")
        raise typer.Exit(1)

    api_key = get_api_key()
    if not api_key and not preview:
        console.print("[red]No API key found.[/red] Set GEMINI_API_KEY or run [green]logwhisper setup[/green] first.")
        raise typer.Exit(1)

    project_ctx = get_project_context()

    # Load log lines from file if provided
    log_lines: list[str] = []
    if file:
        if not file.is_file():
            console.print(f"[red]File not found: {escape(str(file))}[/red]")
            raise typer.Exit(1)
        try:
            with file.open("rb") as stream:
                stream.seek(max(0, os.fstat(stream.fileno()).st_size - 65536))
                lines = stream.read(65536).decode("utf-8", errors="replace").splitlines()
        except OSError as exc:
            console.print(f"[red]Failed to read file: {escape(str(exc))}[/red]")
            raise typer.Exit(1)
        log_lines = [ln.strip() for ln in lines if ln.strip()][-100:]
        console.print(f"[dim]Loaded {len(log_lines)} lines from {escape(file.name)}[/dim]")

    if preview:
        from agent.chat import _format_chat_context
        from agent.privacy import redact
        typer.echo(redact(_format_chat_context(project_ctx, log_lines, error), api_key or ""))
        return

    agent = ChatAgent(api_key=api_key, model=model)

    if error:
        _chat_once(agent, error, log_lines, project_ctx, console)
        return

    _run_chat_repl(agent, log_lines, project_ctx, console)


def _print_reply(console: Console, response: str) -> None:
    from rich.panel import Panel
    from rich.text import Text

    console.print("[dim]" + ("-" * 60) + "[/dim]")
    console.print(Panel(Text(response), border_style="cyan", padding=(1, 2)))
    console.print("[dim]" + ("-" * 60) + "[/dim]")


def _chat_once(agent: Any, question: str, log_lines: list[str], project_ctx: dict, console: Console) -> None:
    console.print("[dim]Analyzing...[/dim]")
    response = asyncio.run(agent.ask(question, log_lines=log_lines, project_context=project_ctx))
    _print_reply(console, response)
    if response.startswith("Error:"):
        raise typer.Exit(1)


def _run_chat_repl(agent: Any, log_lines: list[str], project_ctx: dict, console: Console) -> None:
    """Run an interactive REPL chat session."""
    from rich.panel import Panel

    console.print(
        Panel(
            "[bold cyan]Log Whisperer Chat[/bold cyan]\n"
            "[dim]Paste an error or ask anything about your project.[/dim]\n"
            "[dim]Commands: [bold]exit[/bold] to quit · [bold]context[/bold] to see project context[/dim]",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    console.print()

    if project_ctx["project_type"] != "unknown":
        console.print(f"[dim]Project: {escape(project_ctx['project_type'])}[/dim]")
        if project_ctx["files"]:
            console.print(f"[dim]Files available: {escape(', '.join(project_ctx['files']))}[/dim]")
    console.print()

    while True:
        try:
            user_input = console.input("[bold cyan]>[/bold cyan] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Goodbye![/dim]")
            break

        if not user_input:
            continue

        cmd = user_input.lower()
        if cmd in ("exit", "quit", "q"):
            console.print("[dim]Goodbye![/dim]")
            break

        if cmd == "context":
            if project_ctx["project_type"] == "unknown":
                console.print("[dim]No project context available.[/dim]")
            else:
                from rich.text import Text

                for fname, snippet in project_ctx["files"].items():
                    console.print(f"\n[bold]{escape(fname)}:[/bold]")
                    console.print(Text(snippet[:500], style="dim"))
            console.print()
            continue

        console.print("[dim]Thinking...[/dim]")
        try:
            response = asyncio.run(
                agent.ask(user_input, log_lines=log_lines, project_context=project_ctx)
            )
        except KeyboardInterrupt:
            console.print("\n[dim]Cancelled.[/dim]")
            continue
        except Exception as exc:
            console.print(f"[red]Error: {escape(str(exc))}[/red]")
            continue

        _print_reply(console, response)
