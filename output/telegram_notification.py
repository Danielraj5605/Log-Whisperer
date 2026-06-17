"""Telegram notifications — send formatted alert messages via Bot API."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Send formatted alerts to a Telegram chat via Bot API."""

    def __init__(self, bot_token: str | None = None, chat_id: str | None = None) -> None:
        self.bot_token = bot_token or ""
        self.chat_id = chat_id or ""
        self._enabled = bool(self.bot_token and self.chat_id)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def notify(self, finding: dict[str, Any], project: str = "") -> bool:
        """
        Send an alert to the configured Telegram chat (async, non-blocking).

        Returns True if sent successfully, False otherwise.
        """
        if not self._enabled:
            return False

        message = self._format_message(finding, project=project or "unknown")
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                return True
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Telegram HTTP error: %s — %s",
                exc.response.status_code,
                exc.response.text,
            )
            return False
        except Exception as exc:
            logger.error("Telegram notification failed: %s", exc)
            return False

    def send_text(self, text: str) -> bool:
        """Send a plain text message synchronously (used only in setup/test commands)."""
        if not self._enabled:
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
        }

        try:
            response = httpx.post(url, json=payload, timeout=10.0)
            response.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Telegram send_text failed: %s", exc)
            return False

    def _format_message(self, finding: dict[str, Any], project: str = "") -> str:
        """Format a finding as a Telegram message."""
        confidence = finding.get("confidence", "medium").upper()
        source = finding.get("source", "unknown")
        trigger = finding.get("trigger", "alert")
        root_signal = finding.get("root_signal", "unknown")[:200]
        caused_by = finding.get("caused_by", "")
        action = finding.get("action", "")
        blast_radius = finding.get("blast_radius", "")
        evidence: list[str] = finding.get("evidence", [])
        timestamp = finding.get("timestamp", "")[:19]

        # Emoji by severity
        emoji = "🔴" if confidence == "HIGH" else "🟡" if confidence == "MEDIUM" else "🔵"

        lines = [
            f"{emoji} <b>Log Whisperer Alert</b>",
            f"",
            f"<b>Project:</b> {project} | <b>Source:</b> {source}",
            f"<b>Trigger:</b> {trigger}",
            f"<b>Severity:</b> {confidence}",
            f"<b>Time:</b> {timestamp}",
        ]

        if caused_by:
            lines.append(f"")
            lines.append(f"<b>Caused by:</b> {caused_by}")

        if action:
            lines.append(f"")
            lines.append(f"<b>Action:</b> {action}")

        if blast_radius:
            lines.append(f"")
            lines.append(f"<b>Blast Radius:</b> {blast_radius}")

        if evidence:
            lines.append(f"")
            lines.append(f"<b>Evidence:</b>")
            for ev in evidence[:3]:
                lines.append(f"  • {ev[:150]}")

        lines.append(f"")
        lines.append(f"<b>Root Signal:</b>")
        lines.append(f"{root_signal}")

        return "\n".join(lines)
