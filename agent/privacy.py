"""Redact common credential formats before storage and external delivery."""
import os
import re

_AUTH = re.compile(r"(?i)\b(Bearer|Basic)\s+[^\s,;'\x22]+")
_SECRET = re.compile(
    r"(?i)(['\x22]?(?:[\w-]*(?:password|passwd|secret|token|api[_-]?key)|authorization|cookie|set-cookie)"
    r"['\x22]?\s*[:=]\s*)(?:\x22[^\x22\n]*\x22|'[^'\n]*'|[^\s,;&]+)"
)
_URL = re.compile(r"(://[^/\s:@]+:)[^@\s/]+@")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")


def redact(text: str, *secrets: str) -> str:
    for secret in (*secrets, os.getenv("GEMINI_API_KEY"), os.getenv("TELEGRAM_BOT_TOKEN")):
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?im)\b((?:set-cookie|cookie):)[^\r\n]+", r"\1 [redacted]", text)
    text = _AUTH.sub(r"\1 [redacted]", text)
    text = _SECRET.sub(r"\1[redacted]", text)
    text = _URL.sub(r"\1[redacted]@", text)
    return _EMAIL.sub("[email]", text)
