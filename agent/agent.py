"""AI agent — assembles context, calls Gemini LLM, returns structured finding."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx

AGENT_NAME = "Nexus"

logger = logging.getLogger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"

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
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
        text = text.rstrip("`").strip()
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


class GeminiAgent:
    """LLM agent using Google Gemini API via httpx."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = GEMINI_DEFAULT_MODEL,
        base_url: str = GEMINI_BASE_URL,
    ) -> None:
        # Read key from GEMINI_API_KEY env var
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.model = model
        self.base_url = base_url.rstrip("/")

    def _build_url(self) -> str:
        return f"{self.base_url}/models/{self.model}:generateContent?key={self.api_key}"

    def _build_payload(self, user_content: str) -> dict[str, Any]:
        return {
            "system_instruction": {
                "parts": [{"text": SYSTEM_PROMPT}]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user_content}],
                }
            ],
            "generationConfig": {
                "maxOutputTokens": 1000,
                "temperature": 0.3,
            },
        }

    def _parse_response(self, data: dict[str, Any]) -> str:
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            return ""

    async def analyze(self, trigger_event: dict[str, Any]) -> dict[str, Any]:
        """Send trigger event to Gemini LLM and return structured finding."""
        start = time.time()
        user_content = _format_context(trigger_event)

        url = self._build_url()
        payload = self._build_payload(user_content)

        timeout = 30.0
        last_error: str = "Unknown error"

        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(url, json=payload)
                    if response.status_code == 429:
                        wait = 2 ** attempt
                        logger.warning("Gemini rate-limited, retrying in %ss", wait)
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
                last_error = str(exc)
                logger.error("Gemini LLM call failed (attempt %d): %s", attempt + 1, exc)
                if attempt == 2:
                    return _make_error_finding(trigger_event, last_error)
                await asyncio.sleep(2 ** attempt)
        else:
            return _make_error_finding(trigger_event, "Max retries exceeded")

        content = self._parse_response(data)
        if not content:
            return _make_error_finding(trigger_event, "Unexpected Gemini response format")

        finding = _parse_finding(content)
        elapsed_ms = int((time.time() - start) * 1000)
        finding["latency_ms"] = elapsed_ms
        finding["trigger"] = trigger_event["rule"]
        finding["source"] = trigger_event["log_obj"]["source"]
        finding["timestamp"] = trigger_event["log_obj"]["ts"]
        finding["alert_id"] = f"LW-{time.strftime('%Y%m%d-%H%M%S')}"
        finding["suppressed_count"] = 0  # filled by caller
        return finding


# Backwards-compatible alias — existing code referencing MiniMaxAgent continues to work
MiniMaxAgent = GeminiAgent


# ── Fallback finding factories ─────────────────────────────────────────────────


def _make_timeout_finding(trigger_event: dict[str, Any]) -> dict[str, Any]:
    return {
        "alert_id": f"LW-{time.strftime('%Y%m%d-%H%M%S')}",
        "timestamp": trigger_event["log_obj"]["ts"],
        "source": trigger_event["log_obj"]["source"],
        "trigger": trigger_event["rule"],
        "confidence": "low",
        "root_signal": trigger_event["log_obj"]["raw"][:200],
        "caused_by": "Gemini LLM call timed out after 30s",
        "contributing_factors": [],
        "blast_radius": "unknown",
        "evidence": [],
        "action": "Check Gemini API availability or increase timeout",
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
        "action": "Check GEMINI_API_KEY env var and Gemini service status",
        "kb_used": False,
        "latency_ms": 0,
        "suppressed_count": 0,
    }
