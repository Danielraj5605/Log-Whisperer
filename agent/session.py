"""Session management — API key, config, subprocess lifecycle."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml


def get_global_config_dir() -> Path:
    """Get the global config directory for logwhisper."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / ".config"))
    else:
        base = Path.home() / ".config"
    return base / "logwhisper"


def get_global_credentials_path() -> Path:
    """Get path to the global credentials file."""
    return get_global_config_dir() / "credentials.json"


def get_global_api_key() -> tuple[str | None, str | None]:
    """Get API key and provider from global config. Returns (api_key, provider)."""
    creds_path = get_global_credentials_path()
    if creds_path.exists():
        try:
            with open(creds_path, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("api_key", ""), data.get("provider", "gemini")
        except (json.JSONDecodeError, OSError):
            pass
    return None, "gemini"


def save_global_api_key(api_key: str, provider: str = "gemini") -> None:
    """Save API key and provider to global config directory (securely)."""
    config_dir = get_global_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)

    creds_path = get_global_credentials_path()
    # Load existing to preserve telegram config
    existing = {}
    if creds_path.exists():
        try:
            with open(creds_path, encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    existing["api_key"] = api_key
    existing["provider"] = provider

    with open(creds_path, "w", encoding="utf-8") as f:
        json.dump(existing, f)

    # Restrict permissions on Unix
    if sys.platform != "win32":
        try:
            os.chmod(creds_path, 0o600)
        except OSError:
            pass


def get_api_key_and_provider() -> tuple[str | None, str | None]:
    """Get API key and its detected provider from env or config.

    Returns (api_key, provider) — provider is 'gemini' or 'unknown'.
    Priority: GEMINI_API_KEY env > saved config > project config.
    """
    # 1. Environment variable (highest priority)
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if gemini_key:
        return gemini_key, "gemini"

    # 2. Global credentials file
    global_key, saved_provider = get_global_api_key()
    if global_key:
        return global_key, saved_provider or "gemini"

    # 3. Project .logwhisper.yaml
    project_key = get_project_api_key()
    if project_key:
        return project_key, "gemini"

    return None, "unknown"


def get_fallback_key_and_provider() -> tuple[str | None, str | None]:
    """Get the fallback API key and provider (kept for API compatibility).

    Since we are now Gemini-only, there is no separate fallback key.
    Returns (None, 'unknown') unless a second key is stored in credentials.
    """
    # Check credentials file for an explicit gemini_api_key secondary slot
    creds_path = get_global_credentials_path()
    if creds_path.exists():
        try:
            with open(creds_path, encoding="utf-8") as f:
                data = json.load(f)
            fallback_key = data.get("gemini_api_key", "").strip()
            if fallback_key:
                return fallback_key, "gemini"
        except (json.JSONDecodeError, OSError):
            pass

    return None, "unknown"


def get_api_key() -> str | None:
    """Get API key only (legacy compatibility)."""
    key, _ = get_api_key_and_provider()
    return key


def get_provider() -> str:
    """Get the configured LLM provider from global config or env."""
    _, provider = get_api_key_and_provider()
    return provider


def get_project_api_key() -> str | None:
    """Get API key from project .logwhisper.yaml."""
    config_path = Path.cwd() / ".logwhisper.yaml"
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
            return config.get("llm", {}).get("api_key", "")
        except (yaml.YAMLError, OSError):
            pass
    return None


def prompt_api_key() -> str:
    """Prompt user for a Gemini API key and save globally."""
    from rich.console import Console
    from rich.prompt import Prompt

    console = Console()
    console.print("\n[cyan]No API key found.[/cyan]")
    console.print("Run [green]logwhisper setup[/green] to configure your API key once.\n")

    key = Prompt.ask("Enter your Gemini API key", password=True)

    if not key.strip():
        raise ValueError("API key cannot be empty")

    save_global_api_key(key.strip(), provider="gemini")
    console.print("[green]API key saved![/green]\n")
    return key.strip()


def ensure_api_key(existing_key: str | None = None) -> str:
    """Get API key from env/global, or prompt user if missing."""
    if existing_key and existing_key.strip():
        return existing_key.strip()

    key = get_api_key()
    if key:
        return key

    return prompt_api_key()


# --- Telegram helpers ---


def get_telegram_config() -> dict[str, str]:
    """Get Telegram bot token and chat ID from global config."""
    creds_path = get_global_credentials_path()
    if creds_path.exists():
        try:
            with open(creds_path, encoding="utf-8") as f:
                data = json.load(f)
            return {
                "bot_token": data.get("telegram_bot_token", ""),
                "chat_id": data.get("telegram_chat_id", ""),
            }
        except (json.JSONDecodeError, OSError):
            pass
    return {"bot_token": "", "chat_id": ""}


def save_telegram_config(bot_token: str, chat_id: str) -> None:
    """Save Telegram config to global credentials file."""
    config_dir = get_global_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)

    creds_path = get_global_credentials_path()
    # Load existing to preserve API key
    existing = {}
    if creds_path.exists():
        try:
            with open(creds_path, encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    existing["telegram_bot_token"] = bot_token.strip()
    existing["telegram_chat_id"] = chat_id.strip()

    with open(creds_path, "w", encoding="utf-8") as f:
        json.dump(existing, f)

    if sys.platform != "win32":
        try:
            os.chmod(creds_path, 0o600)
        except OSError:
            pass


def clear_telegram_config() -> None:
    """Remove Telegram config from global credentials."""
    creds_path = get_global_credentials_path()
    if creds_path.exists():
        try:
            with open(creds_path, encoding="utf-8") as f:
                data = json.load(f)
            data.pop("telegram_bot_token", None)
            data.pop("telegram_chat_id", None)
            with open(creds_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except (json.JSONDecodeError, OSError):
            pass
