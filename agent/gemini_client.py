"""Thin async client for the Gemini generateContent endpoint, shared by the alert agent and chat."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3


class GeminiError(Exception):
    """A failed Gemini call. The message never contains the API key."""


class GeminiTimeout(GeminiError):
    """The request timed out on every attempt."""


def generation_config(model: str, max_tokens: int, temperature: float, json_output: bool = False) -> dict[str, Any]:
    cfg: dict[str, Any] = {"maxOutputTokens": max_tokens, "temperature": temperature}
    if json_output:
        cfg["responseMimeType"] = "application/json"
    # 2.5-flash "thinks" by default and thinking tokens count against maxOutputTokens,
    # which truncates or empties the visible answer. Turn thinking off for it.
    if "2.5-flash" in model:
        cfg["thinkingConfig"] = {"thinkingBudget": 0}
    return cfg


def extract_text(data: dict[str, Any]) -> str:
    """Return the visible answer text from a generateContent response ('' if none)."""
    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
    return "".join(
        p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought")
    ).strip()


def _block_reason(data: dict[str, Any]) -> str:
    reason = (data.get("promptFeedback") or {}).get("blockReason")
    if reason:
        return f"prompt blocked by Gemini ({reason})"
    candidates = data.get("candidates") or []
    finish = (candidates[0] or {}).get("finishReason") if candidates else None
    if finish and finish not in ("STOP", "MAX_TOKENS"):
        return f"response stopped by Gemini ({finish})"
    return "empty response from Gemini"


async def generate(
    api_key: str,
    model: str,
    payload: dict[str, Any],
    *,
    base_url: str = GEMINI_BASE_URL,
    timeout: float = 30.0,
) -> str:
    """POST to generateContent and return the response text.

    The API key is sent in a header (never in the URL), so it cannot leak through
    exception messages or logs. Raises GeminiError / GeminiTimeout with a safe message.
    """
    if not api_key:
        raise GeminiError("no API key configured (set GEMINI_API_KEY or run `logwhisper setup`)")

    url = f"{base_url.rstrip('/')}/models/{model}:generateContent"
    headers = {"x-goog-api-key": api_key}
    last_error = "unknown error"
    timed_out = False

    for attempt in range(_MAX_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
            if response.status_code in _RETRYABLE_STATUS:
                last_error = f"HTTP {response.status_code} from Gemini"
                timed_out = False
            elif response.status_code >= 400:
                # Client errors (bad key, bad model, bad request) will not fix themselves
                raise GeminiError(f"HTTP {response.status_code} from Gemini: {_error_message(response)}")
            else:
                data = response.json()
                text = extract_text(data)
                if not text:
                    raise GeminiError(_block_reason(data))
                return text
        except httpx.TimeoutException:
            last_error = f"request timed out after {timeout:g}s"
            timed_out = True
        except httpx.HTTPError as exc:
            # Class name only: httpx messages can embed the request URL
            last_error = f"network error ({type(exc).__name__})"
            timed_out = False
        except ValueError:
            raise GeminiError("Gemini returned a non-JSON response") from None

        if attempt < _MAX_ATTEMPTS - 1:
            wait = 2 ** attempt
            logger.warning("Gemini call failed (%s), retrying in %ss", last_error, wait)
            await asyncio.sleep(wait)

    raise (GeminiTimeout if timed_out else GeminiError)(last_error)


def _error_message(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", {}).get("message", ""))[:300] or "request rejected"
    except (ValueError, AttributeError):
        return "request rejected"
