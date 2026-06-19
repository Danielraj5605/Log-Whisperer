"""
LogWhisper splash screen.

Exported as show_splash() for use by the REPL.
Run standalone with:  python logwhisper_splash.py
Requires:  pip install rich
"""

from __future__ import annotations

import os
import sys
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.align import Align
from rich.text import Text

# Force UTF-8 on Windows so block/unicode chars render correctly
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

# Use legacy_windows=False so Rich uses ANSI sequences instead of the
# Win32 console API (which is limited to cp1252 and breaks on block chars).
_console = Console(legacy_windows=False)

# ASCII-safe creature art — works on every terminal encoding
CREATURE_LINES = [
    r"   /\     /\   ",
    r"  (  o   o  )  ",
    r"   \  ^ ^  /   ",
    r"   /|     |\   ",
    r"  / |     | \  ",
    r" /__|_____|__\ ",
]
CREATURE = "\n".join(CREATURE_LINES)


def show_splash(
    *,
    console: Console | None = None,
    api_key: str | None = None,
    provider: str = "gemini",
    cwd: str | None = None,
    parts: dict | None = None,
) -> None:
    """
    Render the Log Whisperer splash screen.

    Args:
        console:   Rich Console instance (uses module-level one if None)
        api_key:   Configured API key (None = not configured)
        provider:  LLM provider name, e.g. "gemini"
        cwd:       Current working directory string
        parts:     Detected project services dict {name: {command, ...}}
    """
    con = console or _console
    cwd = cwd or os.getcwd()

    # ── Left column: avatar + identity ────────────────────────────────────
    if api_key:
        key_preview = f"{api_key[:8]}... [{provider}]"
        key_line = Text(f"[+] key active  {key_preview}", style="green")
    else:
        key_line = Text("[!] no API key -- run /setup", style="red")

    left = Group(
        Align.center(Text(CREATURE, style="bold cyan")),
        Text(""),
        Align.center(Text("* NEXUS -- Online *", style="bold white")),
        Align.center(Text("LogWhisper v0.1.0", style="dim")),
        Align.center(key_line),
        Align.center(Text(cwd, style="dim")),
    )

    # ── Right column: quick-start tips + detected services ────────────────
    right_items: list = [
        Text("Quick commands", style="bold cyan"),
        Text(""),
        Text("/run           Start all detected services", style="dim"),
        Text("/watch <file>  Tail a log file for anomalies", style="dim"),
        Text("/chat <msg>    Ask the AI about an error", style="dim"),
        Text("/setup         Configure API key or Telegram", style="dim"),
        Text("/help          Show all commands", style="dim"),
    ]

    if parts:
        right_items += [
            Text(""),
            Text("Detected services", style="bold cyan"),
        ]
        for name, info in parts.items():
            right_items.append(
                Text(f"  {name}  {info.get('command', '')}", style="dim")
            )
    else:
        right_items += [
            Text(""),
            Text("Detected services", style="bold cyan"),
            Text("  No project detected in this directory", style="dim"),
            Text("  Use /run-frontend or /run-backend manually", style="dim"),
        ]

    right = Group(*right_items)

    # ── Layout ────────────────────────────────────────────────────────────
    grid = Table.grid(expand=True, padding=(0, 2, 0, 0))
    grid.add_column(ratio=1)
    grid.add_column(ratio=2)
    grid.add_row(left, right)

    con.print()
    con.print(
        Panel(
            grid,
            title="[bold cyan]LogWhisper[/bold cyan]",
            title_align="left",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    con.print()


if __name__ == "__main__":
    # Demo mode — shows placeholder data
    show_splash(
        api_key="AQ.Ab8RN6IUk_demo",
        provider="gemini",
        cwd="~/EdgeCraft",
        parts={
            "frontend": {"command": "npm run dev"},
            "backend": {"command": "uvicorn main:app"},
        },
    )
