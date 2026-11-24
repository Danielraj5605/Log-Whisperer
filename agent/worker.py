"""Background worker that consumes the trigger queue and runs the agent."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

AGENT_NAME = "Nexus"


async def agent_worker(
    queue: asyncio.Queue[dict[str, Any]],
    agent,  # GeminiAgent or similar
    on_finding: Callable[[dict[str, Any]], Coroutine[Any, Any, None]],
    suppressed_ref: list[int] | None = None,
) -> None:
    """
    Background worker that:
    1. Consumes trigger events from the queue
    2. Calls the LLM agent
    3. Calls on_finding with the result

    Args:
        queue: asyncio.Queue of trigger events
        agent: LLM agent (must have .analyze(trigger_event) -> dict method)
        on_finding: async callback to handle each finding
        suppressed_ref: optional shared list [suppressed_count]; copied into each finding
            (on_finding may overwrite it with a fresher value)
    """
    while True:
        trigger_event = await queue.get()
        try:
            finding = await agent.analyze(trigger_event)
            if suppressed_ref:
                finding["suppressed_count"] = suppressed_ref[0]
            await on_finding(finding)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("%s worker error: %s", AGENT_NAME, exc)
        finally:
            queue.task_done()
