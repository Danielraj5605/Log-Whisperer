"""Windows toast notifications via winotify."""

from __future__ import annotations

import logging
import sys
import threading
from typing import Any

try:
    import winotify
    WINOTIFY_AVAILABLE = sys.platform == "win32"
except ImportError:
    WINOTIFY_AVAILABLE = False

from output import _fmt

logger = logging.getLogger(__name__)


class WindowsNotifier:
    """Send Windows toast notifications when an alert fires."""

    def __init__(self) -> None:
        self._available = WINOTIFY_AVAILABLE
        if not self._available and sys.platform == "win32":
            logger.warning("winotify not installed — Windows notifications disabled")

    def notify(self, finding: dict[str, Any]) -> None:
        """
        Show a Windows toast notification for an alert finding.

        winotify shells out to PowerShell, which takes a noticeable fraction of a second,
        so the toast is shown from a background thread to keep the event loop responsive.

        Args:
            finding: Structured alert dict from the agent.
        """
        if not self._available:
            return

        title = self._title(finding)
        body = self._body(finding)
        threading.Thread(
            target=self._show, args=(title, body), name="logwhisper-toast", daemon=True
        ).start()

    @staticmethod
    def _show(title: str, body: str) -> None:
        try:
            winotify.Notification(app_id="Log Whisperer", title=title, msg=body).show()
        except Exception as exc:
            logger.error("Failed to show Windows notification: %s", exc)

    def _title(self, finding: dict[str, Any]) -> str:
        confidence = _fmt.confidence(finding)
        source = _fmt.text(finding, "source", "unknown")
        trigger = _fmt.text(finding, "trigger", "alert")
        emoji = "🔴" if confidence == "high" else "🟡" if confidence == "medium" else "🔵"
        return f"{emoji} Log Whisperer — {trigger} ({source})"

    def _body(self, finding: dict[str, Any]) -> str:
        lines = [_fmt.text(finding, "root_signal", "unknown", 100)]
        caused = _fmt.text(finding, "caused_by", "", 100)
        action = _fmt.text(finding, "action", "", 100)
        if caused:
            lines.append(caused)
        if action:
            lines.append(f"→ {action}")
        return "\n".join(lines)
