"""Session management — API key, config, subprocess lifecycle."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from config import load



def get_global_config_dir() -> Path:
    """Get the global config directory for logwhisper."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or (Path.home() / ".config"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "logwhisper"


def get_global_credentials_path() -> Path:
    """Get path to the global credentials file."""
    return get_global_config_dir() / "credentials.json"


def _read_credentials() -> dict[str, Any]:
    """Load the credentials file ({} if missing, unreadable or malformed)."""
    path = get_global_credentials_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_credentials(data: dict[str, Any]) -> None:
    """Atomically write the credentials file, owner-read/write only from the moment it exists."""
    config_dir = get_global_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    path = get_global_credentials_path()

    # mkstemp creates the file with mode 0600, so the secret is never world-readable
    fd, tmp_name = tempfile.mkstemp(dir=config_dir, prefix=".credentials-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def get_global_api_key() -> tuple[str | None, str | None]:
    """Get API key and provider from global config. Returns (api_key, provider)."""
    data = _read_credentials()
    key = data.get("api_key")
    if isinstance(key, str) and key.strip():
        return key.strip(), data.get("provider") or "gemini"
    return None, "gemini"


def save_global_api_key(api_key: str, provider: str = "gemini") -> None:
    """Save API key and provider to global config directory (securely)."""
    existing = _read_credentials()  # preserve telegram config
    existing["api_key"] = api_key
    existing["provider"] = provider
    _write_credentials(existing)


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
    fallback_key = _read_credentials().get("gemini_api_key")
    if isinstance(fallback_key, str) and fallback_key.strip():
        return fallback_key.strip(), "gemini"
    return None, "unknown"


def get_api_key() -> str | None:
    """Get API key only (legacy compatibility)."""
    key, _ = get_api_key_and_provider()
    return key


def get_provider() -> str:
    """Get the configured LLM provider from global config or env."""
    _, provider = get_api_key_and_provider()
    return provider or "unknown"


def get_project_api_key() -> str | None:
    """Get API key from project .logwhisper.yaml (supports ${ENV_VAR} references)."""
    key = load()["llm"]["api_key"]
    return key.strip() or None


def mask_secret(secret: str, visible: int = 4) -> str:
    """Show only the last few characters of a secret, e.g. '••••••••abcd'."""
    if len(secret) <= visible * 2:
        return "•" * len(secret)
    return "•" * 8 + secret[-visible:]


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
    data = _read_credentials()
    return {
        "bot_token": os.getenv("TELEGRAM_BOT_TOKEN") or str(data.get("telegram_bot_token") or ""),
        "chat_id": os.getenv("TELEGRAM_CHAT_ID") or str(data.get("telegram_chat_id") or ""),
    }


def save_telegram_config(bot_token: str, chat_id: str) -> None:
    """Save Telegram config to global credentials file."""
    existing = _read_credentials()  # preserve API key
    existing["telegram_bot_token"] = bot_token.strip()
    existing["telegram_chat_id"] = chat_id.strip()
    _write_credentials(existing)


def clear_telegram_config() -> None:
    """Remove Telegram config from global credentials."""
    if not get_global_credentials_path().exists():
        return
    data = _read_credentials()
    data.pop("telegram_bot_token", None)
    data.pop("telegram_chat_id", None)
    _write_credentials(data)
