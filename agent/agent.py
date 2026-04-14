"""AI agent — assembles context, calls LLM, returns structured finding."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx


logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Log Whisperer, a live log analysis AI agent embedded in a developer's terminal.

Your job: given a window of recent log lines and a trigger event, reason about what is
actually happening — not just what the error says, but why it is occurring and what it means.

Always respond in this exact JSON structure:
{
  "root_signal": "The single most important log line or event",
  "caused_by": "Plain English explanation of root cause (1-2 sentences)",
  "confidence": "high | medium | low",
  "contributing_factors": ["factor 1", "factor 2"],
  "blast_radius": "Which services or users are affected",
  "evidence": ["timestamp: log line", "timestamp: log line"],
  "action": "One concrete step the engineer should take right now",
  "kb_used": true | false
}

Rules:
- Never invent log lines. Only cite lines from the provided window.
- If you are uncertain, set confidence to "low" and explain why.
- Keep caused_by under 2 sentences.
- Keep action specific and immediately actionable.
- Do not repeat the raw error message in caused_by — explain the cause behind it.
"""


def _format_context(trigger_event: dict[str, Any]) -> str:
    rule = trigger_event["rule"]
    source = trigger_event["log_obj"]["source"]
    ts = trigger_event["log_obj"]["ts"]
    stats = trigger_event["stats"]
    window = trigger_event["buffer_window"]

    lines = []
    for entry in window[-100:]:  # last 100 lines max for context
        lvl = entry.get("level", "INFO")
        t = entry.get("ts", "")
        raw = entry.get("raw", "")
        lines.append(f"[{lvl}] {t}  {raw}")

    log_block = "\n".join(lines) if lines else "(no context)"

    return f"""TRIGGER: {rule} fired on source "{source}" at {ts}

STATS:
- Errors in last 10s: {stats.get('errors_per_10s', 0)}
- Unique error types: {stats.get('unique_error_types', 0)}
- Lines in window: {stats.get('total_lines', 0)}

RECENT LOG WINDOW (last 60 seconds):
{log_block}

Analyze this situation and return your finding as JSON."""


def _parse_finding(raw_text: str) -> dict[str, Any]:
    """Extract JSON from LLM response text."""
    text = raw_text.strip()
    # Try to find JSON block
    start = text.find("{")
    end = text.rfind("}") + 1
    if start != -1 and end != 0:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            pass
    # Fallback: return as-is wrapped
    return {
        "root_signal": raw_text[:200],
        "caused_by": "Failed to parse LLM response",
        "confidence": "low",
        "contributing_factors": [],
        "blast_radius": "unknown",
        "evidence": [],
        "action": "Check LLM response manually",
        "kb_used": False,
    }


class MiniMaxAgent:
    """LLM agent using MiniMax's OpenAI-compatible API."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "MiniMax-Text-01",
        base_url: str = "https://api.minimax.chat/v1",
    ) -> None:
        self.api_key = api_key or os.environ.get("MINIMAX_API_KEY", "")
        self.model = model
        self.base_url = base_url.rstrip("/")

    async def analyze(self, trigger_event: dict[str, Any]) -> dict[str, Any]:
        """Send trigger event to MiniMax LLM and return structured finding."""
        start = time.time()
        user_content = _format_context(trigger_event)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": 1000,
            "temperature": 0.3,
        }

        timeout = 30.0
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers=headers,
                        json=payload,
                    )
                    if response.status_code == 429:
                        wait = 2 ** attempt
                        await asyncio.sleep(wait)
                        continue
                    response.raise_for_status()
                    data = response.json()
                    break
            except httpx.TimeoutException:
                if attempt == 2:
                    return _make_timeout_finding(trigger_event)
                await asyncio.sleep(2 ** attempt)
            except Exception as exc:
                logger.error("LLM call failed: %s", exc)
                if attempt == 2:
                    return _make_error_finding(trigger_event, str(exc))
                await asyncio.sleep(2 ** attempt)
        else:
            return _make_error_finding(trigger_event, "Max retries exceeded")

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            return _make_error_finding(trigger_event, "Unexpected response format")

        finding = _parse_finding(content)
        elapsed_ms = int((time.time() - start) * 1000)
        finding["latency_ms"] = elapsed_ms
        finding["trigger"] = trigger_event["rule"]
        finding["source"] = trigger_event["log_obj"]["source"]
        finding["timestamp"] = trigger_event["log_obj"]["ts"]
        finding["alert_id"] = f"LW-{time.strftime('%Y%m%d-%H%M%S')}"
        finding["suppressed_count"] = 0  # filled by caller
        return finding


def _make_timeout_finding(trigger_event: dict[str, Any]) -> dict[str, Any]:
    return {
        "alert_id": f"LW-{time.strftime('%Y%m%d-%H%M%S')}",
        "timestamp": trigger_event["log_obj"]["ts"],
        "source": trigger_event["log_obj"]["source"],
        "trigger": trigger_event["rule"],
        "confidence": "low",
        "root_signal": trigger_event["log_obj"]["raw"][:200],
        "caused_by": "LLM call timed out after 30s",
        "contributing_factors": [],
        "blast_radius": "unknown",
        "evidence": [],
        "action": "Check LLM service availability",
        "kb_used": False,
        "latency_ms": 30000,
        "suppressed_count": 0,
    }


def _make_error_finding(trigger_event: dict[str, Any], error: str) -> dict[str, Any]:
    return {
        "alert_id": f"LW-{time.strftime('%Y%m%d-%H%M%S')}",
        "timestamp": trigger_event["log_obj"]["ts"],
        "source": trigger_event["log_obj"]["source"],
        "trigger": trigger_event["rule"],
        "confidence": "low",
        "root_signal": trigger_event["log_obj"]["raw"][:200],
        "caused_by": f"LLM error: {error}",
        "contributing_factors": [],
        "blast_radius": "unknown",
        "evidence": [],
        "action": "Check MiniMax API key and service status",
        "kb_used": False,
        "latency_ms": 0,
        "suppressed_count": 0,
    }
