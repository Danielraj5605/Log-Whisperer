"""Run orchestrator — detect multi-service project, spawn all services, monitor logs."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from rich.columns import Columns
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from agent.deps import check_service_deps
from agent.detect import detect_project_parts, detect_project_type
from agent.pipeline import Monitor
from agent.procs import ainput, iter_lines, spawn, terminate
from agent.session import ensure_api_key, get_api_key
from buffer.parser import parse
from output.dashboard import Dashboard

console = Console()
logger = logging.getLogger("logwhisper")

# ANSI escape code strip pattern (matches SGR and other common sequences)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

# Pattern to extract localhost / 0.0.0.0 URLs printed by dev servers
_URL_RE = re.compile(
    r"(https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d{1,5})?(?:/\S*)?)",
    re.IGNORECASE,
)


async def run(auto_install: bool = False, skip_deps_check: bool = False) -> None:
    """Main run flow: detect all project parts → check deps → spawn all services → monitor logs."""

    # --- Project Detection ---
    parts = detect_project_parts()

    if not parts:
        project_type = detect_project_type()
        console.print("[yellow]No project parts detected.[/yellow]")
        console.print(f"[dim]Project type:[/dim] {project_type or 'unknown'}")
        console.print("[dim]Run your dev server manually or cd into a project.[/dim]\n")
        parts = await _prompt_for_services()
        if not parts:
            console.print("[dim]No services to run.[/dim]")
            return
    else:
        console.print(f"[green]Detected {len(parts)} service(s):[/green]")
        for name, info in parts.items():
            console.print(f"  [cyan]{escape(name)}[/cyan] — {escape(info['command'])}")
        console.print()

    service_names = list(parts.keys())

    # --- Create per-service log directories ---
    for info in parts.values():
        Path(info["log_path"]).parent.mkdir(parents=True, exist_ok=True)

    # --- API Key ---
    api_key = ensure_api_key(get_api_key())

    # --- Print startup header ---
    _print_header(parts)

    # --- Dependency check ---
    if not skip_deps_check:
        await asyncio.to_thread(_check_and_install_deps, parts, auto_install, console)

    # --- Setup Monitoring ---
    dashboard = Dashboard(service_names)
    monitor = Monitor(
        api_key,
        on_finding=lambda finding: (
            dashboard.add_alert(finding),
            dashboard.increment_errors(finding.get("source") or "unknown"),
        ),
    )

    processes: dict[str, asyncio.subprocess.Process] = {}
    log_files: dict[str, Any] = {}
    tasks: list[asyncio.Task] = []

    try:
        dashboard.start()

        # --- Spawn all service subprocesses ---
        for name, info in parts.items():
            log_files[name] = open(info["log_path"], "a", encoding="utf-8", errors="replace")
            try:
                proc = await spawn(info["command"])
                processes[name] = proc
                dashboard.set_running(name)
                console.print(f"[green]Started {escape(name)}[/green] (PID {proc.pid})")
            except Exception as exc:
                console.print(f"[red]Failed to start {escape(name)}: {escape(str(exc))}[/red]")
                dashboard.set_error(name)

        if not processes:
            console.print("[red]No service could be started.[/red]")
            return

        console.print()

        # Track which services have already announced their URL (avoid spam)
        announced_urls: set[str] = set()

        async def read_service_output(name: str, proc: asyncio.subprocess.Process) -> None:
            """Read subprocess output, display, tee to log file, feed to trigger engine."""
            log_file = log_files.get(name)
            async for decoded in iter_lines(proc.stdout):
                # Strip ANSI escape codes (e.g. from Vite/Node color output)
                clean = _ANSI_RE.sub("", decoded)
                if not clean.strip():
                    continue

                dashboard.append_log(name, clean)
                dashboard.increment_lines(name)

                if log_file:
                    log_file.write(clean + "\n")
                    log_file.flush()

                # --- Detect real URL from server output and announce it ---
                url_match = _URL_RE.search(clean)
                if url_match:
                    real_url = url_match.group(1).rstrip("/").replace("0.0.0.0", "localhost")
                    key = f"{name}:{real_url}"
                    if key not in announced_urls:
                        announced_urls.add(key)
                        console.print()
                        console.print(
                            Panel(
                                f"[bold green]🌐  {escape(name)}[/bold green]  is ready\n\n"
                                f"   [bold white underline]{escape(real_url)}[/bold white underline]",
                                border_style="green",
                                padding=(0, 2),
                                expand=False,
                            )
                        )
                        console.print()

                # Feed to trigger engine (evaluates BEFORE pushing to the buffer)
                monitor.ingest(parse(clean, source=name, adapter="subprocess"))

            dashboard.set_stopped(name)

        # --- Start all async tasks ---
        worker_task = monitor.start_worker()
        tasks.append(worker_task)
        output_tasks = [
            asyncio.create_task(read_service_output(name, proc))
            for name, proc in processes.items()
        ]
        tasks.extend(output_tasks)

        # Run until every service has exited (or Ctrl+C); the worker runs forever, so it
        # must not be part of the wait.
        await asyncio.gather(*output_tasks)

        # Give queued alerts a moment to be analyzed and shown before shutting down
        try:
            await asyncio.wait_for(monitor.queue.join(), timeout=60)
        except asyncio.TimeoutError:
            pass

    except (asyncio.CancelledError, KeyboardInterrupt):
        console.print("\n[dim]Interrupted...[/dim]")
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(terminate(p) for p in processes.values()), return_exceptions=True)
        for lf in log_files.values():
            try:
                lf.close()
            except Exception:
                pass
        dashboard.stop()
        console.print("[dim]Goodbye![/dim]")


def _check_and_install_deps(parts: dict[str, dict], auto_install: bool, console: Console) -> None:
    """Check dependencies for all services, prompt or auto-install if missing."""
    from rich.prompt import Confirm

    services_needing_deps: list[tuple[str, Path]] = []
    for name, info in parts.items():
        service_path = Path(info["path"])
        result = check_service_deps(name, service_path)
        if result.missing:
            services_needing_deps.append((name, service_path))

    if not services_needing_deps:
        return

    console.print()
    console.print(
        f"[yellow]⚠ Dependencies may be missing for {len(services_needing_deps)} service(s).[/yellow]"
    )
    for name, path in services_needing_deps:
        console.print(f"  [cyan]{escape(name)}[/cyan]  ({escape(str(path))})")

    if auto_install:
        console.print("\n[cyan]Auto-installing dependencies...[/cyan]")
        run_install(services_needing_deps, console)
        return

    try:
        answer = Confirm.ask("\nDependencies missing. Install now?", default=False)
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Skipping install.[/dim]")
        return

    if answer:
        run_install(services_needing_deps, console)


def run_install(services_needing_deps: list[tuple[str, Path]], console: Console) -> None:
    """Run install for each service (blocking; call from a worker thread)."""
    import subprocess

    for name, path in services_needing_deps:
        result = check_service_deps(name, path)
        if result.manager == "unknown":
            console.print(f"[dim]  {escape(name)}: no recognized package manager, skipping[/dim]")
            continue

        console.print(f"  [cyan]{escape(name)}[/cyan]  installing via {result.manager}...")

        try:
            proc = subprocess.run(
                result.install_command(),
                cwd=str(path),
                capture_output=True,
                text=True,
                timeout=300,
            )
            if proc.returncode == 0:
                console.print(f"    [green]✓ {escape(name)} installed[/green]")
            else:
                console.print(f"    [red]✗ {escape(name)} install failed[/red]")
                if proc.stderr:
                    console.print(f"      [dim]{escape(proc.stderr[:200])}[/dim]")
        except subprocess.TimeoutExpired:
            console.print(f"    [red]✗ {escape(name)} install timed out[/red]")
        except OSError:
            console.print(f"    [red]✗ {result.manager} not found — is it installed?[/red]")


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
        "    ▀▀   ▀▀    ▀▀   ▀▀  ",
    ]

    avatar_panel = Panel(
        Text("\n".join(avatar_lines), style="bold #00bcd4"),
        border_style="#00bcd4",
        padding=(1, 1),
    )

    status_lines = [
        "Starting LogWhisper",
        "",
        f"{len(parts)} service(s) to monitor",
        "",
        os.getcwd(),
    ]
    status_panel = Panel(
        Text("\n".join(status_lines)),
        border_style="#00bcd4",
        padding=(1, 2),
    )

    console.print()
    console.print(Columns([avatar_panel, status_panel], expand=True))
    console.print()

    for name, info in parts.items():
        console.print(f"  [cyan]{escape(name)}[/cyan]  {escape(info['command'])}")
        console.print(f"           logs: {escape(info['log_path'])}")
        url = info.get("url")
        if url:
            console.print(
                f"           [dim]expected url →[/dim] "
                f"[bold cyan underline]{escape(url)}[/bold cyan underline]"
                f"  [dim](will confirm when server starts)[/dim]"
            )
    console.print()


async def _prompt_for_services() -> dict[str, dict]:
    """Prompt user for services when auto-detection finds nothing."""
    console.print("[yellow]Enter services manually.[/yellow]")
    console.print("Format: <name>:<command>  (one per line, empty line to finish)\n")

    services: dict[str, dict] = {}
    while True:
        try:
            line = await ainput(lambda: console.input("[cyan]>[/cyan] "))
        except EOFError:
            break
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
        else:
            console.print("[dim]Expected <name>:<command>[/dim]")
    return services
