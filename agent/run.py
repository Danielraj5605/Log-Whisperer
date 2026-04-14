"""Run orchestrator — detect multi-service project, spawn all services, monitor logs."""

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
from rich.text import Text

from agent.agent import MiniMaxAgent
from agent.detect import detect_project_parts, detect_project_type, get_project_name
from agent.session import ensure_api_key, get_api_key, get_telegram_config
from buffer.parser import parse
from buffer.ring_buffer import RingBuffer
from ingest.file_adapter import FileAdapter
from output.terminal import (
    ServiceStatusPanel,
    print_alert,
    print_service_log,
)
from output.telegram_notification import TelegramNotifier
from output.windows_notification import WindowsNotifier
from trigger.cooldown import CooldownTracker
from trigger.engine import TriggerEngine
from trigger.rules import CriticalKeywordRule, HttpErrorRule, NewErrorTypeRule, RateSpikeRule

console = Console()

# ANSI escape code strip pattern (matches SGR and other common sequences)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")




async def run() -> None:
    """Main run flow: detect all project parts → spawn all services → monitor logs."""
    # --- API Key ---
    existing_key = get_api_key()
    api_key = ensure_api_key(existing_key)

    # --- Project Detection ---
    parts = detect_project_parts()

    if not parts:
        project_type = detect_project_type()
        console.print(f"[yellow]No project parts detected.[/yellow]")
        console.print(f"[dim]Project type:[/dim] {project_type or 'unknown'}")
        console.print("[dim]Run your dev server manually or cd into a project.[/dim]\n")
        parts = await _prompt_for_services()
    else:
        console.print(f"[green]Detected {len(parts)} service(s):[/green]")
        for name, info in parts.items():
            console.print(f"  [cyan]{name}[/cyan] — {info['command']}")
        console.print()

    service_names = list(parts.keys())

    # --- Create per-service log directories ---
    for name, info in parts.items():
        log_dir = Path(info["log_path"]).parent
        log_dir.mkdir(parents=True, exist_ok=True)

    # --- Print startup header ---
    _print_header(parts)

    # --- Setup Monitoring ---
    buffer = RingBuffer(maxlen=500)
    cooldowns = CooldownTracker()
    agent_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=50)
    engine = TriggerEngine(
        rules=[
            NewErrorTypeRule(),
            RateSpikeRule(threshold=3, window_seconds=10),
            CriticalKeywordRule(),
            HttpErrorRule(),
        ],
        cooldowns=cooldowns,
        agent_queue=agent_queue,
    )
    agent = MiniMaxAgent(api_key=api_key)
    windows_notifier = WindowsNotifier()

    # Telegram notifier — only if configured
    telegram_config = get_telegram_config()
    telegram_notifier = None
    if telegram_config["bot_token"] and telegram_config["chat_id"]:
        telegram_notifier = TelegramNotifier(
            bot_token=telegram_config["bot_token"],
            chat_id=telegram_config["chat_id"],
        )

    finding_count = [0]

    # --- Service status tracker ---
    status_panel = ServiceStatusPanel(service_names)

    async def on_finding(finding: dict[str, Any]) -> None:
        finding_count[0] += 1
        source = finding.get("source", "unknown")
        status_panel.increment_errors(source)

        try:
            windows_notifier.notify(finding)
        except Exception as exc:
            import logging
            logging.getLogger("logwhisper").warning("Windows notification failed: %s", exc)

        if telegram_notifier:
            try:
                telegram_notifier.notify(finding, project=get_project_name())
            except Exception as exc:
                import logging
                logging.getLogger("logwhisper").warning("Telegram notification failed: %s", exc)

        print_alert(finding, suppressed_count=engine.suppression_count)

    # --- Spawn all service subprocesses ---
    processes: dict[str, asyncio.subprocess.Process] = {}
    log_files: dict[str, Any] = {}

    for name, info in parts.items():
        log_path = Path(info["log_path"])
        lf = open(log_path, "a", encoding="utf-8", errors="replace")
        log_files[name] = lf

        try:
            proc = await asyncio.create_subprocess_shell(
                info["command"],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            processes[name] = proc
            status_panel.set_running(name)
            console.print(f"[green]Started {name}[/green] (PID {proc.pid})")
        except Exception as exc:
            console.print(f"[red]Failed to start {name}: {exc}[/red]")
            status_panel.set_error(name)

    console.print()
    console.print(status_panel.render())
    console.print()

    # --- Per-service: read output → tee to file + print with [SERVICE] tag ---
    async def read_service_output(name: str, proc: asyncio.subprocess.Process) -> None:
        """Read subprocess output, display with [SERVICE] tag, tee to log file, feed to trigger."""
        log_file = log_files.get(name)
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

            # Strip ANSI escape codes (e.g. from Vite/Node color output)
            clean = _ANSI_RE.sub("", decoded)
            if not clean:
                continue

            print_service_log(name, clean)

            if log_file:
                log_file.write(clean + "\n")
                log_file.flush()

            status_panel.increment_lines(name)

            # Feed to trigger engine immediately so errors are caught live
            log_obj = parse(clean, source=name, adapter="subprocess")
            # Evaluate BEFORE pushing so is_new_signature() checks the right state
            engine.evaluate(log_obj, buffer)
            buffer.push(log_obj)

        status_panel.set_stopped(name)

    # --- Per-service: tail log file → feed to trigger engine ---
    async def monitor_service(name: str, log_path: str) -> None:
        """Tail a per-service log file and feed to trigger engine."""
        adapter = FileAdapter(
            path=Path(log_path),
            poll_ms=100,
            source=name,
        )

        while True:
            try:
                async for log_obj in adapter.stream():
                    buffer.push(log_obj)
                    engine.evaluate(log_obj, buffer)
            except FileNotFoundError:
                await asyncio.sleep(0.5)
            except Exception:
                await asyncio.sleep(0.5)

    # --- Start all async tasks ---
    worker_task = asyncio.create_task(
        _agent_worker(agent_queue, agent, on_finding, finding_count)
    )

    output_tasks = [
        asyncio.create_task(read_service_output(name, proc))
        for name, proc in processes.items()
    ]

    monitor_tasks = [
        asyncio.create_task(monitor_service(name, info["log_path"]))
        for name, info in parts.items()
    ]

    all_tasks = [worker_task, *output_tasks, *monitor_tasks]

    try:
        await asyncio.gather(*all_tasks)
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted...[/dim]")
    finally:
        for t in all_tasks:
            t.cancel()
        for name, proc in processes.items():
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
        for lf in log_files.values():
            try:
                lf.close()
            except Exception:
                pass
        console.print("[dim]Goodbye![/dim]")


def _print_header(parts: dict[str, dict]) -> None:
    """Print the startup header panel."""
    import os

    # Pixel avatar using block characters
    avatar_lines = [
        "      ▄▄▄▄▄▄▄▄▄▄▄▄      ",
        "    ▄▄█            █▄▄  ",
        "    ██   ▄      ▄   ██  ",
        "  ▄▄██              ██▄▄",
        "  ████              ████",
        "  ▀▀██              ██▀▀",
        "    ██   ▄▄    ▄▄   ██  ",
        "    ▀▀   ▀▀    ▀▀   ▀▀  "
    ]

    cwd = os.getcwd()

    # Build avatar panel
    avatar_panel = Panel(
        Text("\n".join(avatar_lines), style="bold #00bcd4"),
        border_style="#00bcd4",
        padding=(1, 1),
    )

    # Build status panel
    status_lines = []
    status_lines.append("[bold white]Starting LogWhisper[/bold white]")
    status_lines.append("")
    status_lines.append(f"[cyan]{len(parts)} service(s) to monitor[/cyan]")
    status_lines.append("")
    status_lines.append(f"[dim]{cwd}[/dim]")

    status_panel = Panel(
        Text("\n".join(status_lines)),
        border_style="#00bcd4",
        padding=(1, 2),
    )

    # Side-by-side using Columns
    from rich.columns import Columns
    banner_content = Columns([avatar_panel, status_panel], expand=True)

    console.print()
    console.print(banner_content)
    console.print()

    for name, info in parts.items():
        console.print(f"  [cyan]{name}[/cyan]  {info['command']}")
        console.print(f"           logs: {info['log_path']}")
    console.print()


async def _prompt_for_services() -> dict[str, dict]:
    """Prompt user for services when auto-detection finds nothing."""
    console = Console()
    console.print("[yellow]No project detected. Enter services manually.[/yellow]")
    console.print("Format: <name>:<command>  (one per line, empty line to finish)\n")

    services = {}
    while True:
        line = await asyncio.get_event_loop().run_in_executor(
            None, lambda: console.input("[cyan]>[/cyan] ")
        )
        line = line.strip()
        if not line:
            break
        if ":" in line:
            name, cmd = line.split(":", 1)
            name = name.strip()
            cmd = cmd.strip()
            if name and cmd:
                services[name] = {
                    "name": name,
                    "path": ".",
                    "log_path": f"logs/{name}/app.log",
                    "type": "custom",
                    "command": cmd,
                }
    return services


async def _agent_worker(
    queue: asyncio.Queue[dict[str, Any]],
    agent: MiniMaxAgent,
    on_finding: Any,
    finding_count_ref: list[int],
) -> None:
    """Background worker — consume trigger queue, call agent, invoke on_finding."""
    while True:
        try:
            trigger_event = await asyncio.wait_for(queue.get(), timeout=5.0)
        except asyncio.TimeoutError:
            continue

        try:
            finding = await agent.analyze(trigger_event)
            finding["suppressed_count"] = finding_count_ref[0]
            await on_finding(finding)
        except Exception as exc:
            import logging

            logging.getLogger("logwhisper").error("Agent worker error: %s", exc)
        finally:
            queue.task_done()
