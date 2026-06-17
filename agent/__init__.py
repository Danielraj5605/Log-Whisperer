"""Log Whisperer CLI — main entry point."""

from __future__ import annotations

# Must be first — ensure project root is on path before any other imports
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import json
import logging
import os
from typing import Any

import typer

from agent.agent import GeminiAgent
from agent.detect import get_project_name
from agent.repl import REPLSession
from agent.session import get_telegram_config
from agent.shell import Shell
from agent.worker import agent_worker
from buffer.parser import parse
from buffer.ring_buffer import RingBuffer
from config import load as load_config
from ingest.file_adapter import FileAdapter
from output.terminal import LiveStatusBar, console, print_alert
from output.telegram_notification import TelegramNotifier
from output.windows_notification import WindowsNotifier
from trigger.cooldown import CooldownTracker
from trigger.engine import TriggerEngine
from trigger.rules import (
    CriticalKeywordRule,
    HttpErrorRule,
    MetricThresholdRule,
    ClusterFormationRule,
    SilenceAnomalyRule,
    NewErrorTypeRule,
    RateSpikeRule,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("logwhisper")

app = typer.Typer(add_completion=False)


@app.callback(invoke_without_command=True)
def main_callback(ctx: typer.Context) -> None:
    """Called when no subcommand is given — launches the interactive REPL."""
    if ctx.invoked_subcommand is None:
        asyncio.run(REPLSession().run())


@app.command()
def run(
    cwd: Path | None = typer.Option(None, "--cwd", help="Project root directory"),
    auto_install: bool = typer.Option(
        False, "--auto-install", help="Automatically install missing dependencies without prompting"
    ),
    skip_deps_check: bool = typer.Option(
        False, "--skip-deps-check", help="Skip dependency check before starting services"
    ),
) -> None:
    """Auto-detect project, start dev server, and monitor logs (legacy)."""
    import os

    if cwd:
        os.chdir(cwd)

    from agent.run import run as run_session
    from config import load as load_config

    cfg = load_config()
    deps_cfg = cfg.get("deps", {})
    # CLI flags override config; config defaults are False
    final_auto_install = auto_install or deps_cfg.get("auto_install", False)
    final_skip_check = skip_deps_check or deps_cfg.get("skip_check", False)

    asyncio.run(run_session(auto_install=final_auto_install, skip_deps_check=final_skip_check))


@app.command()
def project(
    paths: list[Path] = typer.Argument(..., help="Project root directories to watch"),
    parallel: bool = typer.Option(False, "--parallel", "-p", help="Launch all windows at once"),
) -> None:
    """
    Launch multiple independent Log Whisperer instances, one per project.

    Each project runs in its own terminal window with full Log Whisperer
    functionality: auto-detected services, log monitoring, and AI alerts.

    Example:
        log-whisperer project ./backend ./frontend
        log-whisperer project /path/to/project1 /path/to/project2 --parallel
    """
    import subprocess

    processes = []
    for project_path in paths:
        abs_path = project_path.resolve()
        if not abs_path.exists():
            typer.echo(f"[red]Path not found: {abs_path}[/red]")
            continue

        project_name = abs_path.name

        cmd = [
            sys.executable, "-m", "log_whisperer",
            "run", "--cwd", str(abs_path),
        ]

        if sys.platform == "win32":
            CREATE_NEW_CONSOLE = 0x00000010
            proc = subprocess.Popen(
                    cmd,
                    cwd=str(abs_path),
                    creationflags=CREATE_NEW_CONSOLE,
                )
        elif sys.platform == "darwin":
            # macOS: open a new Terminal window
            osa_script = f'tell app "Terminal" to do script "{" ".join(cmd)}"'
            proc = subprocess.Popen(
                ["osascript", "-e", osa_script],
                cwd=str(abs_path),
            )
        else:
            # Linux: try gnome-terminal → xterm → tmux
            try:
                proc = subprocess.Popen(
                    ["gnome-terminal", "--", "bash", "-c", f"{' '.join(cmd)}; exec bash"],
                    cwd=str(abs_path),
                )
            except FileNotFoundError:
                try:
                    proc = subprocess.Popen(
                        ["xterm", "-hold", "-e", " ".join(cmd)],
                        cwd=str(abs_path),
                    )
                except FileNotFoundError:
                    # tmux fallback: open a new window in the current session
                    session = f"lw-{project_name}"
                    proc = subprocess.Popen(
                        ["tmux", "new-window", "-n", session, " ".join(cmd)],
                        cwd=str(abs_path),
                    )

        processes.append((project_name, proc))
        typer.echo(f"[green]Started {project_name}[/green] (PID {proc.pid})")

    if not processes:
        typer.echo("[yellow]No projects started.[/yellow]")
        raise typer.Exit(1)

    if not parallel:
        typer.echo("\n[dim]Waiting for all projects... Ctrl+C to stop all.[/dim]")
        for name, proc in processes:
            proc.wait()
    else:
        typer.echo(f"[green]{len(processes)} project(s) launched.[/green]")


AGENT_QUEUE_SIZE = 50
BUFFER_MAXLEN = 500


@app.command()
def shell(
    log_file: str | None = typer.Option(
        None, "--log-file", help="Log file path (overrides config default)"
    ),
    model: str = typer.Option(
        "gemini-2.5-flash", "--model", help="Gemini model name"
    ),
    api_key: str | None = None,
    no_notification: bool = typer.Option(
        False, "--no-notification", help="Disable Windows toast notifications"
    ),
) -> None:
    """Interactive mode: run commands and monitor logs in the same terminal."""
    api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        log.error("No API key provided. Set GEMINI_API_KEY or use --api-key")
        raise typer.Exit(1)

    cfg = load_config()
    trigger_cfg = cfg.get("triggers", {})
    rate_cfg = trigger_cfg.get("rate_spike", {})
    crit_cfg = trigger_cfg.get("critical_keyword", {})
    jsonl_cfg = cfg.get("output", {}).get("jsonl", {})
    default_files = cfg.get("watch", {}).get("default_files", [])
    log_path = log_file or (default_files[0] if default_files else "./logs/app.log")

    # Setup log monitoring (runs in background while shell is active)
    buffer = RingBuffer(maxlen=BUFFER_MAXLEN)
    cooldowns = CooldownTracker()
    agent_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=AGENT_QUEUE_SIZE)

    rules = [
        NewErrorTypeRule(),
        RateSpikeRule(
            threshold=rate_cfg.get("threshold", 3),
            window_seconds=rate_cfg.get("window_seconds", 10),
        ),
        CriticalKeywordRule(extra_keywords=crit_cfg.get("extra_keywords", [])),
        HttpErrorRule(),
        MetricThresholdRule(),
        ClusterFormationRule(),
        SilenceAnomalyRule(),
    ]

    engine = TriggerEngine(rules=rules, cooldowns=cooldowns, agent_queue=agent_queue)
    agent = GeminiAgent(api_key=api_key, model=model)
    notifier = None if no_notification else WindowsNotifier()

    # Telegram notifier — only if configured
    telegram_config = get_telegram_config()
    telegram_notifier = None
    if telegram_config["bot_token"] and telegram_config["chat_id"]:
        telegram_notifier = TelegramNotifier(
            bot_token=telegram_config["bot_token"],
            chat_id=telegram_config["chat_id"],
        )

    finding_count = [0]

    async def on_finding(finding: dict[str, Any]) -> None:
        finding_count[0] += 1
        if notifier:
            try:
                notifier.notify(finding)
            except Exception as exc:
                log.warning("Notification failed: %s", exc)
        if telegram_notifier:
            try:
                await telegram_notifier.notify(finding, project=get_project_name())
            except Exception as exc:
                log.warning("Telegram notification failed: %s", exc)
        # Write to JSONL
        if jsonl_cfg.get("enabled", True):
            try:
                jsonl_path = Path(jsonl_cfg.get("path", ".logwhisper/alerts.jsonl"))
                jsonl_path.parent.mkdir(parents=True, exist_ok=True)
                with open(jsonl_path, "a", encoding="utf-8") as jf:
                    jf.write(json.dumps(finding) + "\n")
            except Exception as exc:
                log.warning("JSONL write failed: %s", exc)
        print_alert(finding, suppressed_count=engine.suppression_count)

    # Start log monitoring
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def monitor_logs():
        """Background task: tail the log file and feed to trigger engine."""
        worker_task = asyncio.create_task(
            agent_worker(agent_queue, agent, on_finding, [0])
        )

        adapter = FileAdapter(path=Path(log_path), poll_ms=100)

        async def tail():
            try:
                async for log_obj in adapter.stream():
                    buffer.push(log_obj)
                    engine.evaluate(log_obj, buffer)
            except FileNotFoundError:
                pass  # File not created yet

        tail_task = asyncio.create_task(tail())

        try:
            await asyncio.gather(tail_task, worker_task)
        except asyncio.CancelledError:
            worker_task.cancel()
            tail_task.cancel()

    # Start monitoring in background
    monitor_future = asyncio.ensure_future(monitor_logs())

    # Run shell
    shell_instance = Shell(log_path=log_path)

    try:
        loop.run_until_complete(shell_instance.shell_loop(log_path=log_path))
    except KeyboardInterrupt:
        log.info("Interrupted")
    finally:
        monitor_future.cancel()
        loop.run_until_complete(asyncio.sleep(0.1))
        loop.close()

    log.info("Shutdown complete.")


@app.command()
def watch(
    file: list[Path] = typer.Option(
        None, "--file", "-f", help="Log file(s) to tail"
    ),
    docker: list[str] = typer.Option(
        None, "--docker", "-d", help="Docker container name(s) to watch"
    ),
    poll_ms: int = typer.Option(
        100, "--poll", help="File polling interval in milliseconds"
    ),
    buffer_size: int = typer.Option(
        BUFFER_MAXLEN, "--buffer", "-b", help="Ring buffer max lines"
    ),
    model: str = typer.Option(
        "gemini-2.5-flash", "--model", help="Gemini model name"
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
    """Watch log file(s) in real-time and fire AI-analyzed alerts."""
    api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        log.error("No API key provided. Set GEMINI_API_KEY or use --api-key")
        raise typer.Exit(1)

    cfg = load_config()
    trigger_cfg = cfg.get("triggers", {})
    rate_cfg = trigger_cfg.get("rate_spike", {})
    crit_cfg = trigger_cfg.get("critical_keyword", {})
    jsonl_cfg = cfg.get("output", {}).get("jsonl", {})

    # Use CLI files, else fall back to config defaults
    if not file:
        default_paths = cfg.get("watch", {}).get("default_files", [])
        if default_paths:
            file = [Path(p) for p in default_paths]
        else:
            log.error("Provide at least one source: --file or --docker")
            raise typer.Exit(1)

    if not file and not docker:
        log.error("Provide at least one source: --file or --docker")
        raise typer.Exit(1)

    suppressed_ref: list[int] = [0]

    # Setup
    buffer = RingBuffer(maxlen=buffer_size)
    cooldowns = CooldownTracker()
    agent_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=AGENT_QUEUE_SIZE)

    rules = [
        NewErrorTypeRule(),
        RateSpikeRule(
            threshold=rate_cfg.get("threshold", 3),
            window_seconds=rate_cfg.get("window_seconds", 10),
        ),
        CriticalKeywordRule(extra_keywords=crit_cfg.get("extra_keywords", [])),
        HttpErrorRule(),
        MetricThresholdRule(),
        ClusterFormationRule(),
        SilenceAnomalyRule(),
    ]

    engine = TriggerEngine(rules=rules, cooldowns=cooldowns, agent_queue=agent_queue)
    agent = GeminiAgent(api_key=api_key, model=model)
    windows_notifier = None if no_notification else WindowsNotifier()

    # Telegram notifier — only if configured and not disabled
    telegram_config = get_telegram_config()
    telegram_notifier = None
    if not no_telegram and telegram_config["bot_token"] and telegram_config["chat_id"]:
        telegram_notifier = TelegramNotifier(
            bot_token=telegram_config["bot_token"],
            chat_id=telegram_config["chat_id"],
        )
        log.info("Telegram alerts enabled")

    # Track sources
    sources = []
    if file:
        sources.extend(f.name for f in file)
    if docker:
        sources.extend(docker)

    status = LiveStatusBar(sources=sources)

    finding_count = 0

    async def on_finding(finding: dict[str, Any]) -> None:
        nonlocal finding_count
        finding_count += 1
        suppressed_ref[0] = engine.suppression_count

        # Windows toast
        if windows_notifier:
            try:
                windows_notifier.notify(finding)
            except Exception as exc:
                log.warning("Windows notification failed: %s", exc)

        # Telegram alert
        if telegram_notifier:
            try:
                await telegram_notifier.notify(finding, project=get_project_name())
            except Exception as exc:
                log.warning("Telegram notification failed: %s", exc)

        # Terminal print
        print_alert(finding, suppressed_count=engine.suppression_count)

        # Write to JSONL
        if jsonl_cfg.get("enabled", True):
            try:
                jsonl_path = Path(jsonl_cfg.get("path", ".logwhisper/alerts.jsonl"))
                jsonl_path.parent.mkdir(parents=True, exist_ok=True)
                with open(jsonl_path, "a", encoding="utf-8") as jf:
                    jf.write(json.dumps(finding) + "\n")
            except Exception as exc:
                log.warning("JSONL write failed: %s", exc)

        status.update(
            alert_count=finding_count,
            suppressed_count=engine.suppression_count,
        )

    async def run_live() -> None:
        nonlocal engine

        # Start agent worker
        worker_task = asyncio.create_task(
            agent_worker(agent_queue, agent, on_finding, suppressed_ref)
        )

        # Create adapters
        adapters = []
        for f in (file or []):
            ad = FileAdapter(path=f, poll_ms=poll_ms)
            adapters.append(ad)

        # NOTE: Docker adapter is stubbed for now — file tailing is fully working
        # Docker integration would go here once docker SDK is installed

        async def run_adapter(ad):
            try:
                async for log_obj in ad.stream():
                    buffer.push(log_obj)
                    engine.evaluate(log_obj, buffer)
            except Exception as exc:
                log.error("Adapter error: %s", exc)

        adapter_tasks = [asyncio.create_task(run_adapter(ad)) for ad in adapters]

        async def status_updater() -> None:
            """Periodically update status bar."""
            while True:
                await asyncio.sleep(5)
                status.update(
                    alert_count=finding_count,
                    suppressed_count=engine.suppression_count,
                )

        status_task = asyncio.create_task(status_updater())

        try:
            await asyncio.gather(*adapter_tasks)
        except asyncio.CancelledError:
            worker_task.cancel()
            status_task.cancel()
            for t in adapter_tasks:
                t.cancel()
        finally:
            worker_task.cancel()
            status_task.cancel()

    # Setup signal handlers
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown():
        log.info("Shutting down...")
        for t in asyncio.all_tasks(loop):
            t.cancel()
        loop.stop()

    try:
        loop.run_until_complete(run_live())
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    finally:
        loop.close()


@app.command()
def analyze(
    file: Path = typer.Option(..., "--file", help="Log file to analyze"),
    since: str = typer.Option(None, "--since", help="Filter logs since this time"),
    until: str = typer.Option(None, "--until", help="Filter logs until this time"),
    model: str = typer.Option("gemini-2.5-flash", "--model"),
    api_key: str = typer.Option(None, "--api-key"),
) -> None:
    """Analyze a log file in batch mode (not yet implemented)."""
    typer.echo("Batch analysis not yet implemented. Use: logwhisper watch --file <path>")
    raise typer.Exit(1)


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
    from rich.console import Console
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.prompt import Confirm
    from agent.session import (
        clear_telegram_config,
        get_api_key,
        get_telegram_config,
        save_global_api_key,
        save_telegram_config,
    )
    from output.telegram_notification import TelegramNotifier

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
            console.print(f"[green]API key already configured.[/green]")
            console.print(f"  [{existing_key[:8]}...{existing_key[-4:]}]")
            if not Confirm.ask("Overwrite it?"):
                console.print("[dim]Skipping API key setup.[/dim]\n")
            else:
                _setup_api_key_interactive(console)
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
    from agent.session import save_global_api_key

    console.print("[cyan]Step 1:[/cyan] API Key Setup\n")
    console.print("  Get your key at: [blue]https://aistudio.google.com/app/apikey[/blue]\n")

    key = console.input("Enter your Gemini API key: ").strip()
    if not key:
        console.print("[red]API key cannot be empty. Skipping.[/red]\n")
        return

    save_global_api_key(key.strip(), provider="gemini")
    console.print("[green]API key saved![/green] (gemini-2.5-flash ready)\n")


def _setup_telegram_interactive(console: Console) -> None:
    """Prompt for and save Telegram bot credentials."""
    from agent.session import clear_telegram_config, save_telegram_config
    from output.telegram_notification import TelegramNotifier
    from rich.prompt import Confirm

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
    console.print("  1. Open Telegram → chat with [@BotFather](https://t.me/BotFather)")
    console.print("  2. Send [bold]/newbot[/bold], follow the prompts → copy the bot token")
    console.print("  3. Open your bot's chat → send [bold]/start[/bold]")
    console.print("  4. Forward any message from the bot to @userinfobot → copy your Chat ID\n")

    bot_token = console.input("Bot token (e.g. 123456789:ABCdef...): ")
    chat_id = console.input("Chat ID (e.g. 123456789): ")

    if not bot_token.strip() or not chat_id.strip():
        console.print("[red]Token and chat ID cannot be empty. Skipping Telegram.[/red]\n")
        return

    save_telegram_config(bot_token, chat_id)

    console.print("\n[dim]Sending test message...[/dim]")
    notifier = TelegramNotifier(bot_token=bot_token.strip(), chat_id=chat_id.strip())
    if notifier.send_text("✅ <b>Log Whisperer</b> — Telegram alerts are configured and working!"):
        console.print("[green]Telegram configured! Test message sent.[/green]\n")
    else:
        console.print("[red]Failed to send test message. Check your token and chat ID.[/red]")
        clear_telegram_config()
        console.print("[dim]Telegram setup skipped.[/dim]\n")


def _run_setup_tests(console: Console) -> None:
    """Run validation tests after setup and print results."""
    from agent.session import get_api_key, get_telegram_config
    from output.telegram_notification import TelegramNotifier
    from rich.panel import Panel

    console.print("[cyan]Step 3:[/cyan] Validation")

    # Test API key
    api_key = get_api_key()
    if api_key:
        console.print(f"  [green]✓[/green] API key configured")
    else:
        console.print(f"  [red]✗[/red] No API key — alerts won't work")
        console.print("    Run [green]logwhisper setup --skip-telegram[/green] to add it later\n")
        return

    # Test Telegram
    tg_config = get_telegram_config()
    if tg_config["bot_token"] and tg_config["chat_id"]:
        console.print("  [green]✓[/green] Telegram configured")
    else:
        console.print("  [dim]  – Telegram not configured (optional)\n")

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
    from rich.console import Console
    from rich.table import Table
    from agent.session import clear_telegram_config, get_telegram_config
    from output.telegram_notification import TelegramNotifier

    console = Console()
    config = get_telegram_config()

    if action == "clear":
        clear_telegram_config()
        console.print("[green]Telegram configuration cleared.[/green]")
        return

    if not config["bot_token"] or not config["chat_id"]:
        console.print("[yellow]Telegram not configured.[/yellow]")
        console.print("Run [green]logwhisper setup[/green] to configure.\n")
        raise typer.Exit(1)

    if action == "status":
        table = Table(box=None, show_header=False, padding=(0, 2))
        table.add_column(style="bold cyan")
        table.add_column(style="white")
        table.add_row("Bot token", f"{config['bot_token'][:10]}...{config['bot_token'][-4:]}")
        table.add_row("Chat ID", config["chat_id"])
        table.add_row("Status", "[green]Configured[/green]")
        console.print(table)
        return

    if action == "test":
        console.print("Sending test message...")
        notifier = TelegramNotifier(bot_token=config["bot_token"], chat_id=config["chat_id"])
        if notifier.send_text("✅ <b>Log Whisperer</b> — Test message received!"):
            console.print("[green]Test message sent![/green]")
        else:
            console.print("[red]Failed to send. Check your configuration.[/red]")
            raise typer.Exit(1)
        return

    console.print(f"[red]Unknown action: {action}[/red]")
    console.print("Usage: logwhisper telegram [status|test|clear]")
    raise typer.Exit(1)


@app.command()
def chat(
    error: str | None = typer.Option(
        None, "--error", "-e", help="Single-shot: analyze this error or log excerpt"
    ),
    file: Path | None = typer.Option(
        None, "--file", "-f", help="Load recent log lines from a file for context"
    ),
    provider: str = typer.Option(
        "gemini", "--provider", "-p",
        help="LLM provider (gemini)",
    ),
    model: str | None = typer.Option(
        None, "--model", "-m", help="Model name (defaults to provider's default)",
    ),
) -> None:
    """
    Interactive chat with Log Whisperer — paste errors or ask questions about your project.

    Run without arguments for interactive REPL mode, or use --error for single-shot analysis.

    Provider can be set via --provider or GEMINI_API_KEY env var.
    """
    from agent.chat import ChatAgent, get_project_context
    from agent.session import get_api_key_and_provider, get_fallback_key_and_provider
    from rich.console import Console
    from rich.panel import Panel

    console = Console()

    api_key, env_provider = get_api_key_and_provider()
    fallback_key, fallback_provider = get_fallback_key_and_provider()

    # If the key came from GEMINI_API_KEY env, force provider to gemini.
    if api_key and env_provider == "gemini":
        provider = "gemini"

    if not api_key:
        console.print("[red]No API key found.[/red] Set GEMINI_API_KEY or run [green]logwhisper setup[/green] first.")
        raise typer.Exit(1)

    # Load project context once
    project_ctx = get_project_context()

    # Load log lines from file if provided
    log_lines: list[str] = []
    if file:
        if not file.exists():
            console.print(f"[red]File not found: {file}[/red]")
            raise typer.Exit(1)
        try:
            lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
            log_lines = [ln.strip() for ln in lines if ln.strip()][-100:]
            console.print(f"[dim]Loaded {len(log_lines)} lines from {file.name}[/dim]")
        except Exception as exc:
            console.print(f"[red]Failed to read file: {exc}[/red]")
            raise typer.Exit(1)

    # Single-shot mode
    if error:
        _run_single_shot_chat(error, log_lines, project_ctx, api_key, console, provider, model, fallback_key, fallback_provider)
        return

    # Interactive REPL mode
    _run_chat_repl(log_lines, project_ctx, api_key, console, provider, model, fallback_key, fallback_provider)


def _run_single_shot_chat(
    error: str,
    log_lines: list[str],
    project_ctx: dict,
    api_key: str,
    console: Console,
    provider: str = "gemini",
    model: str | None = None,
    fallback_key: str | None = None,
    fallback_provider: str = "gemini",
) -> None:
    """Run a single-shot chat and print the result."""
    import asyncio
    from agent.chat import ChatAgent
    from rich.panel import Panel

    agent = ChatAgent(api_key=api_key, provider=provider, model=model)

    console.print("[dim]Analyzing...[/dim]")
    try:
        response = asyncio.run(
            agent.ask(error, log_lines=log_lines, project_context=project_ctx,
                     fallback_key=fallback_key, fallback_provider=fallback_provider)
        )
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1)

    console.print("[dim]" + ("-" * 60) + "[/dim]")
    console.print(Panel(response, border_style="cyan", padding=(1, 2)))
    console.print("[dim]" + ("-" * 60) + "[/dim]")


def _run_chat_repl(
    log_lines: list[str],
    project_ctx: dict,
    api_key: str,
    console: Console,
    provider: str = "gemini",
    model: str | None = None,
    fallback_key: str | None = None,
    fallback_provider: str = "gemini",
) -> None:
    """Run an interactive REPL chat session."""
    import asyncio
    from agent.chat import ChatAgent
    from rich.panel import Panel

    agent = ChatAgent(api_key=api_key, provider=provider, model=model)

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
        console.print(f"[dim]Project: {project_ctx['project_type']}[/dim]")
        console.print(f"[dim]Files available: {', '.join(project_ctx['files'].keys())}[/dim]")
    console.print()

    while True:
        try:
            user_input = console.input("[bold cyan]>[/bold cyan] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Goodbye![/dim]")
            break

        if not user_input:
            continue

        cmd = user_input.lower().strip()
        if cmd in ("exit", "quit", "q"):
            console.print("[dim]Goodbye![/dim]")
            break

        if cmd == "context":
            if project_ctx["project_type"] == "unknown":
                console.print("[dim]No project context available.[/dim]")
            else:
                for fname, snippet in project_ctx["files"].items():
                    console.print(f"\n[bold]{fname}:[/bold]")
                    console.print(f"[dim]{snippet[:500]}[/dim]")
            console.print()
            continue

        console.print("[dim]Thinking...[/dim]")
        try:
            response = asyncio.run(
                agent.ask(user_input, log_lines=log_lines, project_context=project_ctx,
                         fallback_key=fallback_key, fallback_provider=fallback_provider)
            )
        except Exception as exc:
            console.print(f"[red]Error: {exc}[/red]")
            continue

        console.print("[dim]" + ("-" * 60) + "[/dim]")
        console.print(Panel(response, border_style="cyan", padding=(1, 2)))
        console.print("[dim]" + ("-" * 60) + "[/dim]")


# Ensure default command when running as console script entry point
if __name__ == "__main__":
    if len(sys.argv) == 1 or (
        len(sys.argv) > 1
        and sys.argv[1]
        not in (
            "run",
            "shell",
            "watch",
            "analyze",
            "setup",
            "telegram",
            "chat",
        )
    ):
        sys.argv[0] = "logwhisper"  # ensure correct program name
    app()
