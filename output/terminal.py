"""Rich terminal output — dashboard, alert display, and live status bar."""

from __future__ import annotations

import sys
import time
from collections import deque
from typing import Any

from rich.color import Color
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

# --- Service color palette ---
SERVICE_COLORS = [
    "cyan",
    "green",
    "magenta",
    "yellow",
    "blue",
    "bright_red",
    "bright_green",
    "bright_magenta",
    "bright_blue",
    "bright_cyan",
]

_SERVICE_COLOR_MAP: dict[str, str] = {}


def _get_service_color(service: str) -> str:
    """Get a consistent color for a service name."""
    if service not in _SERVICE_COLOR_MAP:
        idx = len(_SERVICE_COLOR_MAP) % len(SERVICE_COLORS)
        _SERVICE_COLOR_MAP[service] = SERVICE_COLORS[idx]
    return _SERVICE_COLOR_MAP[service]


# --- Severity helpers ---


def severity_color(confidence: str, trigger: str) -> str:
    if confidence == "high" or trigger == "FATAL":
        return "red bold"
    elif confidence == "medium":
        return "yellow"
    return "cyan"


def severity_emoji(confidence: str, trigger: str) -> str:
    if confidence == "high" or trigger == "FATAL":
        return "🔴"
    elif confidence == "medium":
        return "🟡"
    return "🔵"


# --- Service status panel ---


class ServiceStatusPanel:
    """Top panel showing all detected services and their current status."""

    def __init__(self, services: list[str]) -> None:
        self._services = services
        self._status: dict[str, str] = {s: "⏳ starting" for s in services}
        self._error_count: dict[str, int] = {s: 0 for s in services}
        self._lines_count: dict[str, int] = {s: 0 for s in services}

    def set_running(self, service: str) -> None:
        self._status[service] = "🟢 running"

    def set_error(self, service: str) -> None:
        self._status[service] = "🔴 error"

    def set_stopped(self, service: str) -> None:
        self._status[service] = "⚪ stopped"

    def increment_errors(self, service: str) -> None:
        self._error_count[service] = self._error_count.get(service, 0) + 1

    def increment_lines(self, service: str) -> None:
        self._lines_count[service] = self._lines_count.get(service, 0) + 1

    def render(self) -> Panel:
        table = Table(box=None, show_header=False, padding=(0, 1))
        table.add_column(style="bold", width=14)
        table.add_column(style="white")

        for svc in self._services:
            status = self._status.get(svc, "⏳ starting")
            err = self._error_count.get(svc, 0)
            lines = self._lines_count.get(svc, 0)
            color = _get_service_color(svc)
            table.add_row(
                f"[{color}]{svc}[/{color}]",
                f"{status}  ·  {lines} lines  ·  {err} errors",
            )

        title = f" Services ({len(self._services)}) "
        return Panel(table, title=title, border_style="cyan", padding=(1, 2))


# --- Log stream panel ---


class LogStreamPanel:
    """Rolling log stream with [SERVICE] prefix in color."""

    def __init__(self, max_lines: int = 50) -> None:
        self._max_lines = max_lines
        self._lines: deque[tuple[str, str]] = deque(maxlen=max_lines)  # (service, line)

    def append(self, service: str, line: str) -> None:
        self._lines.append((service, line))

    def render(self) -> Panel:
        if not self._lines:
            content = Text("Waiting for logs...", style="dim")
            return Panel(content, title=" Live Logs ", border_style="cyan", padding=(1, 2))

        text = Text()
        for svc, line in self._lines:
            color = _get_service_color(svc)
            prefix = f"[{color}][{svc}][/{color}]"
            text.append(prefix, style=color)
            text.append("  ")
            text.append(line[:200])
            text.append("\n")

        # Remove trailing newline
        if text.plain.endswith("\n"):
            text._text = text._text[:-1]

        return Panel(
            text,
            title=" Live Logs ",
            border_style="cyan",
            padding=(1, 1),
            height=self._max_lines + 2,
        )


# --- Alert rendering ---


def render_alert(finding: dict[str, Any], suppressed_count: int = 0) -> Panel:
    """Render a single alert as a rich Panel."""
    confidence = finding.get("confidence", "medium").lower()
    trigger = finding.get("trigger", "")
    source = finding.get("source", "unknown")
    root_signal = finding.get("root_signal", "unknown")[:80]
    caused_by = finding.get("caused_by", "")
    evidence = finding.get("evidence", [])
    blast_radius = finding.get("blast_radius", "")
    action = finding.get("action", "")
    kb_match = finding.get("kb_match")
    timestamp = finding.get("timestamp", "")[:19]

    emoji = severity_emoji(confidence, trigger)

    grid = Table(box=None, show_header=False, padding=(0, 1))
    grid.add_column(style="bold cyan")
    grid.add_column(style="white")

    grid.add_row("Source", f"{source}")
    grid.add_row("Trigger", trigger)
    grid.add_row("Root signal", root_signal)
    if caused_by:
        grid.add_row("Caused by", caused_by[:100])
    if evidence:
        ev = evidence[0] if isinstance(evidence, list) else str(evidence)
        grid.add_row("Evidence", str(ev)[:100])
    if blast_radius:
        grid.add_row("Blast radius", blast_radius)
    if action:
        grid.add_row("Action", action[:100])
    if kb_match:
        kb = kb_match if isinstance(kb_match, str) else kb_match.get("title", "")
        grid.add_row("KB match", str(kb)[:60])
    if suppressed_count > 0:
        grid.add_row("Suppressed", f"{suppressed_count} duplicates in cooldown")

    title = f" {emoji} ANOMALY DETECTED  {timestamp}"
    style = severity_color(confidence, trigger)

    return Panel(
        grid,
        title=title,
        border_style=style,
        title_align="left",
        padding=(1, 2),
    )


def print_alert(finding: dict[str, Any], suppressed_count: int = 0) -> None:
    """Print a single alert to the terminal."""
    panel = render_alert(finding, suppressed_count)
    console.print(panel)


# --- Per-service log printer ---


def print_service_log(service: str, line: str) -> None:
    """Print a single log line with colored [SERVICE] prefix."""
    color = _get_service_color(service)
    prefix = f"[{color}][{service}][/{color}]"
    console.print(f"{prefix}  {line}")


# --- Live status bar (updated) ---


class LiveStatusBar:
    """Live updating status bar at the bottom of the terminal."""

    def __init__(
        self,
        sources: list[str] | None = None,
        alert_count: int = 0,
        suppressed_count: int = 0,
        kb_size: int = 0,
    ) -> None:
        self._sources = sources or []
        self._alert_count = alert_count
        self._suppressed_count = suppressed_count
        self._kb_size = kb_size
        self._start_time = time.time()

    def update(
        self,
        *,
        alert_count: int | None = None,
        suppressed_count: int | None = None,
        kb_size: int | None = None,
    ) -> None:
        if alert_count is not None:
            self._alert_count = alert_count
        if suppressed_count is not None:
            self._suppressed_count = suppressed_count
        if kb_size is not None:
            self._kb_size = kb_size

    def render(self) -> Table:
        elapsed = int(time.time() - self._start_time)
        hours, rem = divmod(elapsed, 3600)
        minutes, seconds = divmod(rem, 60)
        uptime = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

        src_label = ", ".join(self._sources) if self._sources else "none"
        status = Table(box=None, show_header=False, padding=(0, 2))
        status.add_column(style="green")
        status.add_column(style="white")
        status.add_row("● Watching", src_label)
        status.add_row(
            "Alerts",
            f"[yellow]{self._alert_count}[/yellow]  "
            f"│  Suppressed: [dim]{self._suppressed_count}[/dim]  "
            f"│  KB: [dim]{self._kb_size}[/dim]  "
            f"│  Uptime: [dim]{uptime}[/dim]",
        )
        return status
