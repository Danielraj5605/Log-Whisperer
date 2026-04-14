"""Chat agent — conversational LLM interface for error analysis and project Q&A."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx


CHAT_SYSTEM_PROMPT = """You are Log Whisperer Chat, an expert developer assistant embedded in a terminal.

You help developers by:
1. Analyzing error logs and explaining root causes in plain, clear language
2. Answering questions about their codebase or project structure
3. Suggesting actionable fixes — not just describing the problem

Guidelines:
- Be direct and practical — developers want solutions, not lecture
- When analyzing errors: cite the specific line or detail that reveals the cause
- When answering project questions: use the provided context if available
- If something is unclear, say so rather than guessing
- Use simple language, avoid jargon unless the user uses it first
"""


def get_project_context() -> dict[str, Any]:
    """
    Detect project type and read relevant source files to give the LLM context.
    Returns a dict with project_type and source_snippets.
    """
    cwd = Path.cwd()
    context: dict[str, Any] = {
        "project_type": "unknown",
        "files": {},
        "package_info": {},
    }

    # Detect Python project
    if (cwd / "pyproject.toml").exists():
        context["project_type"] = "python"
        for name in ["app.py", "main.py", "server.py", "run.py"]:
            fp = cwd / name
            if fp.exists():
                try:
                    content = fp.read_text(encoding="utf-8", errors="replace")
                    context["files"][name] = content[:1500]  # first 1500 chars
                except OSError:
                    pass
        # Try requirements.txt / pyproject.toml
        for req in ["requirements.txt", "pyproject.toml"]:
            fp = cwd / req
            if fp.exists():
                try:
                    context["files"][req] = fp.read_text(encoding="utf-8", errors="replace")[:500]
                except OSError:
                    pass

    # Detect Node project
    elif (cwd / "package.json").exists():
        context["project_type"] = "node"
        try:
            pkg = json.loads((cwd / "package.json").read_text(encoding="utf-8", errors="replace"))
            context["package_info"] = {
                "name": pkg.get("name", ""),
                "scripts": pkg.get("scripts", {}),
                "dependencies": list(pkg.get("dependencies", {}).keys())[:10],
            }
        except (json.JSONDecodeError, OSError):
            pass

    return context


def _format_chat_context(context: dict[str, Any], log_lines: list[str], error: str | None) -> str:
    """Build the user prompt with all available context."""
    parts = []

    # Project context
    if context["project_type"] != "unknown":
        parts.append(f"[PROJECT CONTEXT — {context['project_type']} project]")
        if context["files"]:
            for fname, snippet in context["files"].items():
                parts.append(f"\n--- {fname} ---\n{snippet}")
        if context["package_info"]:
            parts.append(f"\n--- package.json ---\n{json.dumps(context['package_info'], indent=2)}")
        parts.append("")

    # Log lines if available
    if log_lines:
        parts.append("[RECENT LOG LINES]")
        for line in log_lines[-50:]:
            parts.append(line)
        parts.append("")

    # User's error
    if error:
        parts.append(f"[USER'S ERROR/QUESTION]\n{error}\n")

    return "\n".join(parts)


class ChatAgent:
    """Conversational LLM agent — supports MiniMax and Google Gemini."""

    PROVIDER_DEFAULTS = {
        "minimax": {
            "model": "MiniMax-Text-01",
            "base_url": "https://api.minimaxi.com/v1",
            "endpoint": "/text/chatcompletion_v2",
        },
        "gemini": {
            "model": "gemini-2.5-flash",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "endpoint": "generateContent",
        },
    }

    def __init__(
        self,
        api_key: str | None = None,
        provider: str = "minimax",
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        defaults = self.PROVIDER_DEFAULTS.get(provider, self.PROVIDER_DEFAULTS["minimax"])

        self.provider = provider
        self.api_key = api_key or self._resolve_api_key(provider)
        self.model = model or defaults["model"]
        self.base_url = (base_url or defaults["base_url"]).rstrip("/")
        self.endpoint = defaults["endpoint"]

    def _resolve_api_key(self, provider: str) -> str:
        """Resolve API key from environment variables based on provider."""
        if provider == "gemini":
            key = os.environ.get("GEMINI_API_KEY", "").strip()
            if key:
                return key
        # Fall back to MINIMAX_API_KEY for minimax and as general fallback
        return os.environ.get("MINIMAX_API_KEY", "").strip()

    def _build_payload(self, messages: list[dict]) -> dict[str, Any]:
        """Build provider-specific request payload."""
        if self.provider == "gemini":
            # Gemini: uses contents format instead of messages
            contents = []
            for msg in messages:
                role = "model" if msg["role"] == "assistant" else "user"
                contents.append({"role": role, "parts": [{"text": msg["content"]}]})
            return {
                "contents": contents,
                "generationConfig": {"maxOutputTokens": 800, "temperature": 0.4},
            }
        # MiniMax / OpenAI-compatible
        return {
            "model": self.model,
            "messages": messages,
            "max_tokens": 800,
            "temperature": 0.4,
        }

    def _build_headers(self) -> dict[str, str]:
        """Build provider-specific headers."""
        if self.provider == "gemini":
            return {"Content-Type": "application/json"}
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _build_url(self) -> str:
        """Build the full request URL."""
        if self.provider == "gemini":
            return f"{self.base_url}/models/{self.model}:{self.endpoint}?key={self.api_key}"
        return f"{self.base_url}{self.endpoint}"

    def _parse_response(self, data: dict) -> str:
        """Extract text from provider response."""
        if self.provider == "gemini":
            try:
                return data["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError):
                return "Error: Could not parse Gemini response."
        # MiniMax / OpenAI-compatible
        try:
            # MiniMax uses choices[0].messages[0].content (not message.content)
            choices = data.get("choices", [])
            if choices:
                msg = choices[0].get("messages", [])
                if msg:
                    return msg[0].get("content", "")
                # OpenAI-compatible fallback
                return choices[0].get("message", {}).get("content", "")
            return "Error: No choices in response."
        except (KeyError, IndexError):
            return "Error: Could not parse LLM response."

    async def ask(
        self,
        question: str,
        *,
        error: str | None = None,
        log_lines: list[str] | None = None,
        project_context: dict[str, Any] | None = None,
        fallback_key: str | None = None,
        fallback_provider: str = "gemini",
    ) -> str:
        """
        Send a conversational question to the LLM and return the response text.

        Args:
            question: The user's question or request
            error: Optional error log to analyze
            log_lines: Optional list of recent log lines for context
            project_context: Optional project context (from get_project_context)
            fallback_key: API key to use if primary fails (e.g. gemini key when minimax fails)
            fallback_provider: Provider name for the fallback key

        Returns:
            The LLM's response as a plain string.
        """
        if project_context is None:
            project_context = get_project_context()
        if log_lines is None:
            log_lines = []

        context_block = _format_chat_context(project_context, log_lines, error)

        user_content = f"""\
{context_block}

USER QUESTION:
{question}
"""

        messages = [
            {"role": "system", "content": CHAT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        headers = self._build_headers()
        payload = self._build_payload(messages)

        timeout = 30.0
        error_detail = None

        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    url = self._build_url()
                    response = await client.post(url, headers=headers, json=payload)
                    if response.status_code == 429:
                        wait = 2 ** attempt
                        await asyncio.sleep(wait)
                        continue
                    response.raise_for_status()
                    data = response.json()
                    # MiniMax returns errors as HTTP 200 with base_resp.status_code
                    if self.provider == "minimax" and data.get("base_resp", {}).get("status_code") not in (0, 200, "0", "200"):
                        status_code = data["base_resp"]["status_code"]
                        status_msg = data["base_resp"].get("status_msg", "unknown error")
                        if status_code in (2049,):
                            raise Exception(f"MiniMax auth error: {status_msg} (code {status_code})")
                        raise Exception(f"MiniMax error: {status_msg} (code {status_code})")
            except httpx.TimeoutException:
                if attempt == 2:
                    error_detail = "Chat request timed out after 30 seconds."
            except httpx.HTTPStatusError as exc:
                # If primary fails with auth/server error, try fallback immediately
                if fallback_key and exc.response.status_code in (401, 403, 500, 502, 503, 504):
                    return await self._try_fallback(
                        messages=messages,
                        fallback_key=fallback_key,
                        fallback_provider=fallback_provider,
                    )
                if attempt == 2:
                    error_detail = f"Chat request failed: {exc}"
                await asyncio.sleep(2 ** attempt)
            except Exception as exc:
                if attempt == 2:
                    error_detail = f"Chat request failed: {exc}"
                await asyncio.sleep(2 ** attempt)
        else:
            # All retries exhausted — try fallback if available
            if fallback_key:
                return await self._try_fallback(
                    messages=messages,
                    fallback_key=fallback_key,
                    fallback_provider=fallback_provider,
                )
            return f"Error: {error_detail}" if error_detail else "Error: Chat request failed after 3 retries."

        return self._parse_response(data)

    async def _try_fallback(
        self,
        messages: list[dict],
        fallback_key: str,
        fallback_provider: str,
    ) -> str:
        """Attempt the same request with the fallback provider."""
        from agent.session import get_fallback_key_and_provider

        # Build a temporary ChatAgent with fallback credentials
        fallback_defaults = self.PROVIDER_DEFAULTS.get(fallback_provider, self.PROVIDER_DEFAULTS["minimax"])
        fallback_agent = ChatAgent(
            api_key=fallback_key,
            provider=fallback_provider,
            model=fallback_defaults["model"],
            base_url=fallback_defaults["base_url"],
        )

        headers = fallback_agent._build_headers()
        payload = fallback_agent._build_payload(messages)

        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    url = fallback_agent._build_url()
                    response = await client.post(url, headers=headers, json=payload)
                    if response.status_code == 429:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    response.raise_for_status()
                    data = response.json()
                    result = fallback_agent._parse_response(data)
                    if result.startswith("Error:"):
                        raise Exception(result)
                    return result
            except Exception:
                if attempt == 2:
                    return "Error: Both primary and fallback providers failed. Check your API keys."
                await asyncio.sleep(2 ** attempt)

        return "Error: Fallback provider also failed after 3 retries."