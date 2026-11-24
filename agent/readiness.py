"""Report service-announced loopback endpoints with a listening TCP socket."""
import asyncio
import re
from urllib.parse import urlsplit

from rich.console import Console
from rich.text import Text


async def report_endpoint(console: Console, service: str, line: str, announced: set[str]) -> None:
    if not re.search(r"\b(?:listening|running on|serving|local:|ready at|started at)", line, re.I):
        return
    for candidate in re.findall(r"https?://[^\s<>\"']+", line, re.IGNORECASE):
        try:
            url = urlsplit(candidate.rstrip(".,;)"))
            host = url.hostname
            if host not in {"localhost", "0.0.0.0", "127.0.0.1", "::", "::1"} or url.username is not None:
                continue
            host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
            port = url.port or (443 if url.scheme == "https" else 80)
            display_host = f"[{host}]" if ":" in host else host
            endpoint = f"{url.scheme}://{display_host}:{port}"
            if endpoint in announced:
                continue
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=0.3)
            writer.close()
            await writer.wait_closed()
        except (ValueError, OSError, asyncio.TimeoutError):
            continue
        announced.add(endpoint)
        console.print(Text(f"{service} reported {endpoint} (TCP listener confirmed)", style="green"))
