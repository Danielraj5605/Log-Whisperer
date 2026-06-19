"""Chat agent — conversational Gemini interface for error analysis and project Q&A."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx


CHAT_SYSTEM_PROMPT = """You are NEXUS, the AI assistant built into Log Whisperer — an AI-powered terminal tool that monitors logs and analyzes errors in real time using the Gemini API.

== ABOUT LOG WHISPERER ==
Log Whisperer is a CLI tool installed as the `logwhisper` command. It watches log files, detects anomalies, and lets developers chat with an AI about their errors.

== LOG WHISPERER CLI COMMANDS (run from any terminal) ==
  logwhisper               Launch the interactive REPL (this chat interface)
  logwhisper watch         Watch a log file in real time
    --file ./app.log         Path to the log file to tail
    --model gemini-2.5-flash LLM model to use
    --no-notification        Disable Windows toast alerts
  logwhisper chat          One-shot or interactive AI chat about errors
    --error "..."            Analyze a specific error message
    --file ./app.log         Load log lines from a file for context
  logwhisper setup         First-time guided setup (API key + Telegram)
    --skip-telegram          Skip Telegram configuration
    --skip-api-key           Skip API key setup
  logwhisper telegram      Manage Telegram alert integration
    status                   Show current Telegram config
    test                     Send a test message
    clear                    Remove Telegram config
  logwhisper run           Auto-detect project and start monitoring (legacy)
  logwhisper shell         Interactive shell + log monitoring in one terminal

== REPL SLASH COMMANDS (inside this chat interface) ==
  /help                    Show all available commands
  /run                     Start all detected project services with monitoring
  /run-frontend            Start only the frontend service
  /run-backend             Start only the backend service
  /stop                    Stop all running services
  /watch <file>            Tail a log file (e.g. /watch ./logs/app.log)
  /chat <message>          Ask the AI a question (or just type without /chat)
  /setup                   Re-run the setup wizard
  /telegram-setup          Configure Telegram alerts
  /telegram <action>       Manage Telegram: status, test, or clear
  /exit or Ctrl+C          Stop services and exit

== YOUR ROLE ==
You help developers by:
1. Answering questions about Log Whisperer itself using the knowledge above
2. Analyzing error logs and explaining root causes in plain, clear language
3. Answering questions about their codebase or project structure
4. Suggesting actionable fixes — not just describing the problem

IMPORTANT: If the user asks about Log Whisperer, its commands, or how to use it,
answer using the information above — do NOT confuse Log Whisperer with the user's project.

Guidelines:
- Be direct and practical — developers want solutions, not lectures
- When analyzing errors: cite the specific line or detail that reveals the cause
- When answering project questions: use the provided project context if available
- If something is unclear, say so rather than guessing
- Use simple language, avoid jargon unless the user uses it first
"""

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"


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
    """Conversational LLM agent using Google Gemini."""

    def __init__(
        self,
        api_key: str | None = None,
        provider: str = "gemini",   # kept for API compatibility, always Gemini
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.provider = "gemini"
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.model = model or GEMINI_DEFAULT_MODEL
        self.base_url = (base_url or GEMINI_BASE_URL).rstrip("/")

    def _build_url(self) -> str:
        return f"{self.base_url}/models/{self.model}:generateContent?key={self.api_key}"

    def _build_payload(self, messages: list[dict]) -> dict[str, Any]:
        """Convert message list to Gemini contents format."""
        # Extract system message if present
        system_text = ""
        chat_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_text = msg["content"]
            else:
                role = "model" if msg["role"] == "assistant" else "user"
                chat_messages.append({"role": role, "parts": [{"text": msg["content"]}]})

        payload: dict[str, Any] = {
            "contents": chat_messages,
            "generationConfig": {"maxOutputTokens": 800, "temperature": 0.4},
        }
        if system_text:
            payload["system_instruction"] = {"parts": [{"text": system_text}]}
        return payload

    def _parse_response(self, data: dict) -> str:
        """Extract text from Gemini response."""
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            return "Error: Could not parse Gemini response."

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
        Send a conversational question to Gemini and return the response text.

        Args:
            question: The user's question or request
            error: Optional error log to analyze
            log_lines: Optional list of recent log lines for context
            project_context: Optional project context (from get_project_context)
            fallback_key: Unused — kept for API compatibility
            fallback_provider: Unused — kept for API compatibility

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

        payload = self._build_payload(messages)
        error_detail: str | None = None

        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    url = self._build_url()
                    response = await client.post(url, json=payload)
                    if response.status_code == 429:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    response.raise_for_status()
                    data = response.json()
                    return self._parse_response(data)
            except httpx.TimeoutException:
                error_detail = "Chat request timed out after 30 seconds."
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
            except httpx.HTTPStatusError as exc:
                error_detail = f"Chat request failed: HTTP {exc.response.status_code}"
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
            except Exception as exc:
                error_detail = f"Chat request failed: {exc}"
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)

        return f"Error: {error_detail}" if error_detail else "Error: Chat request failed after 3 retries."