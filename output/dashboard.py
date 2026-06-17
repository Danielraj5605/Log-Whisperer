"""Live dashboard — Rich.Live-powered terminal dashboard with live updating panels."""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from output.terminal import _get_service_color

# Agent identity — used in dashboard UI and logging
AGENT_NAME = "Nexus"

console = Console()


class Dashboard:
    """Live-updating terminal dashboard using Rich.Live.

    Shows 4 panels in a stable layout:
    - Top left:  Service status (name, status, line count, error count)
    - Top right: Metrics summary (total errors, uptime, service count)
    - Middle:    Live log stream (rolling, max 40 lines)
    - Bottom:    Alert history (last 5 alerts)
    """

    def __init__(self, service_names: list[str]) -> None:
        self._services = service_names
        self._logs: deque[tuple[str, str]] = deque(maxlen=40)
        self._alerts: deque[dict[str, Any]] = deque(maxlen=5)
        self._service_status: dict[str, str] = {s: "⏳ starting" for s in service_names}
        self._service_lines: dict[str, int] = {s: 0 for s in service_names}
        self._service_errors: dict[str, int] = {s: 0 for s in service_names}
        self._total_errors = 0
        self._start_time = time.time()
        self._live: Live | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the live dashboard display."""
        self._live = Live(
            self._render(),
            console=console,
            refresh_per_second=4,
            transient=True,
        )
        self._live.start()

    def stop(self) -> None:
        """Stop the live dashboard."""
        if self._live:
            self._live.stop()
            self._live = None

    # ── Public API ─────────────────────────────────────────────────────────────

    def append_log(self, service: str, line: str) -> None:
        """Add a log line to the live stream."""
        self._logs.append((service, line))
        self._refresh()

    def add_alert(self, finding: dict[str, Any]) -> None:
        """Add an alert to the history panel."""
        self._alerts.appendleft(finding)
        if len(self._alerts) > 5:
            self._alerts.pop()
        self._refresh()

    def increment_errors(self, service: str) -> None:
        """Increment error count for a service."""
        self._service_errors[service] = self._service_errors.get(service, 0) + 1
        self._total_errors += 1
        self._refresh()

    def increment_lines(self, service: str) -> None:
        """Increment line count for a service."""
        self._service_lines[service] = self._service_lines.get(service, 0) + 1
        self._refresh()

    def set_running(self, service: str) -> None:
        self._service_status[service] = "🟢 running"
        self._refresh()

    def set_error(self, service: str) -> None:
        self._service_status[service] = "🔴 error"
        self._refresh()

    def set_stopped(self, service: str) -> None:
        self._service_status[service] = "⚪ stopped"
        self._refresh()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        if self._live:
            self._live.update(self._render())

    def _render(self) -> Layout:
        layout = Layout()
        layout.split_row(
            Layout(self._render_services_panel(), name="services"),
            Layout(self._render_metrics_panel(), name="metrics"),
        )
        layout.split_row(Layout(self._render_log_panel(), name="logs"))
        layout.split_row(Layout(self._render_alerts_panel(), name="alerts"))
        return layout

    def _render_services_panel(self) -> Panel:
        table = Table(box=None, show_header=False, padding=(0, 1))
        table.add_column(style="bold", width=14)
        table.add_column(style="white")
        for svc in self._services:
            status = self._service_status.get(svc, "⏳")
            err = self._service_errors.get(svc, 0)
            lines = self._service_lines.get(svc, 0)
            color = _get_service_color(svc)
            table.add_row(
                f"[{color}]{svc}[/{color}]",
                f"{status}  ·  {lines} lines  ·  {err} errors",
            )
        return Panel(
            table,
            title=f" Services ({len(self._services)}) — 🔷 {AGENT_NAME} ",
            border_style="cyan",
            padding=(1, 2),
        )

    def _render_metrics_panel(self) -> Panel:
        elapsed = int(time.time() - self._start_time)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        uptime = f"{h:02d}:{m:02d}:{s:02d}"
        table = Table(box=None, show_header=False, padding=(0, 1))
        table.add_column(style="bold cyan")
        table.add_column(style="white")
        table.add_row("Agent", f"[bold cyan]🔷 {AGENT_NAME}[/bold cyan]")
        table.add_row("Total errors", f"[red]{self._total_errors}[/red]")
        table.add_row("Uptime", f"[dim]{uptime}[/dim]")
        table.add_row("Services", f"{len(self._services)}")
        return Panel(table, title=f" {AGENT_NAME} ", border_style="cyan", padding=(1, 2))

    def _render_log_panel(self) -> Panel:
        if not self._logs:
            text = Text("Waiting for logs...", style="dim")
        else:
            text = Text()
            for svc, line in self._logs:
                color = _get_service_color(svc)
                text.append(f"[{color}][{svc}][/{color}]  ", style=color)
                text.append(line[:200])
                text.append("\n")
            if text.plain.endswith("\n"):
                text._text = text._text[:-1]
        return Panel(
            text,
            title=" Live Logs ",
            border_style="cyan",
            padding=(1, 1),
            height=20,
        )

    def _render_alerts_panel(self) -> Panel:
        if not self._alerts:
            text = Text("No alerts yet", style="dim")
            return Panel(text, title=" Alert History ", border_style="cyan", padding=(1, 1))
        table = Table(box=None, show_header=False, padding=(0, 1))
        for alert in self._alerts:
            confidence = alert.get("confidence", "medium").lower()
            trigger = alert.get("trigger", "")
            source = alert.get("source", "unknown")
            root = alert.get("root_signal", "")[:50]
            emoji = "🔴" if confidence == "high" else "🟡"
            table.add_row(
                f"{emoji} [cyan]{source}[/cyan]  {trigger}  {root}"
            )
        return Panel(table, title=" Alert History ", border_style="cyan", padding=(1, 1))