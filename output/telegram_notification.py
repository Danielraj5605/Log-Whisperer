"""Telegram notifications — send formatted alert messages via Bot API."""

from __future__ import annotations

import html
import logging
from typing import Any

import httpx

from output import _fmt

logger = logging.getLogger(__name__)

_MAX_MESSAGE_CHARS = 4096  # Telegram's hard limit per message


class TelegramNotifier:
    """Send formatted alerts to a Telegram chat via Bot API."""

    def __init__(self, bot_token: str | None = None, chat_id: str | None = None) -> None:
        self.bot_token = bot_token or ""
        self.chat_id = chat_id or ""
        self._enabled = bool(self.bot_token and self.chat_id)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _url(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

    def _redact(self, message: str) -> str:
        """Strip the bot token from text before it reaches logs (httpx errors embed the URL)."""
        return message.replace(self.bot_token, "<token>") if self.bot_token else message

    async def notify(self, finding: dict[str, Any], project: str = "") -> bool:
        """
        Send an alert to the configured Telegram chat (async, non-blocking).

        Returns True if sent successfully, False otherwise.
        """
        if not self._enabled:
            return False

        payload = {
            "chat_id": self.chat_id,
            "text": self._format_message(finding, project=project or "unknown"),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(self._url(), json=payload)
                response.raise_for_status()
                return True
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Telegram HTTP error: %s — %s",
                exc.response.status_code,
                self._redact(exc.response.text),
            )
            return False
        except Exception as exc:
            logger.error("Telegram notification failed: %s", self._redact(str(exc)))
            return False

    def send_text(self, text: str) -> bool:
        """Send an HTML-formatted message synchronously (used only in setup/test commands)."""
        if not self._enabled:
            return False

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
        }

        try:
            response = httpx.post(self._url(), json=payload, timeout=10.0)
            response.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Telegram send_text failed: %s", self._redact(str(exc)))
            return False

    def _format_message(self, finding: dict[str, Any], project: str = "") -> str:
        """Format a finding as a Telegram HTML message.

        All dynamic text is HTML-escaped: log lines routinely contain '<', '>' and '&', and
        Telegram rejects the whole message (HTTP 400) if the markup is malformed.
        """
        esc = html.escape
        confidence = _fmt.confidence(finding).upper()
        source = esc(_fmt.text(finding, "source", "unknown", 100))
        trigger = esc(_fmt.text(finding, "trigger", "alert", 100))
        root_signal = esc(_fmt.text(finding, "root_signal", "unknown", 200))
        caused_by = esc(_fmt.text(finding, "caused_by", "", 600))
        action = esc(_fmt.text(finding, "action", "", 400))
        blast_radius = esc(_fmt.text(finding, "blast_radius", "", 300))
        evidence = _fmt.items(finding, "evidence")
        timestamp = esc(_fmt.text(finding, "timestamp", "", 19))

        # Emoji by severity
        emoji = "🔴" if confidence == "HIGH" else "🟡" if confidence == "MEDIUM" else "🔵"

        lines = [
            f"{emoji} <b>Log Whisperer Alert</b>",
            "",
            f"<b>Project:</b> {esc(project[:100])} | <b>Source:</b> {source}",
            f"<b>Trigger:</b> {trigger}",
            f"<b>Severity:</b> {confidence}",
            f"<b>Time:</b> {timestamp}",
        ]

        if caused_by:
            lines += ["", f"<b>Caused by:</b> {caused_by}"]

        if action:
            lines += ["", f"<b>Action:</b> {action}"]

        if blast_radius:
            lines += ["", f"<b>Blast Radius:</b> {blast_radius}"]

        if evidence:
            lines += ["", "<b>Evidence:</b>"]
            lines += [f"  • {esc(ev[:150])}" for ev in evidence[:3]]

        lines += ["", "<b>Root Signal:</b>", root_signal]

        message = "\n".join(lines)
        if len(message) > _MAX_MESSAGE_CHARS:
            # Cut on a line boundary so no tag or entity is split in half
            message = message[:_MAX_MESSAGE_CHARS].rsplit("\n", 1)[0]
        return message
