"""Windows toast notifications via winotify."""

from __future__ import annotations

import logging
from typing import Any

try:
    import winotify
    WINOTIFY_AVAILABLE = True
except ImportError:
    WINOTIFY_AVAILABLE = False

logger = logging.getLogger(__name__)


class WindowsNotifier:
    """Send Windows toast notifications when an alert fires."""

    def __init__(self) -> None:
        self._available = WINOTIFY_AVAILABLE
        if not self._available:
            logger.warning("winotify not installed — Windows notifications disabled")

    def notify(self, finding: dict[str, Any]) -> None:
        """
        Show a Windows toast notification for an alert finding.

        Args:
            finding: Structured alert dict from the agent.
        """
        if not self._available:
            return

        title = self._title(finding)
        body = self._body(finding)

        try:
            toast = winotify.Winotify(
                app_id="Log Whisperer",
                title=title,
                text=body,
            )
            toast.show()
        except Exception as exc:
            logger.error("Failed to show Windows notification: %s", exc)

    def _title(self, finding: dict[str, Any]) -> str:
        confidence = finding.get("confidence", "medium").upper()
        source = finding.get("source", "unknown")
        trigger = finding.get("trigger", "alert")
        emoji = "🔴" if confidence == "HIGH" else "🟡" if confidence == "MEDIUM" else "🔵"
        return f"{emoji} Log Whisperer — {trigger} ({source})"

    def _body(self, finding: dict[str, Any]) -> str:
        root = finding.get("root_signal", "unknown")[:100]
        caused = finding.get("caused_by", "")
        action = finding.get("action", "")
        lines = [root]
        if caused:
            lines.append(caused[:100])
        if action:
            lines.append(f"→ {action[:100]}")
        return "\n".join(lines)
