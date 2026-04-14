"""Background worker that consumes the trigger queue and runs the agent."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


async def agent_worker(
    queue: asyncio.Queue[dict[str, Any]],
    agent,  # MiniMaxAgent or similar
    on_finding: Callable[[dict[str, Any]], Coroutine[Any, Any, None]],
    suppressed_ref: list[int],
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
        suppressed_ref: shared list [suppressed_count] for updates
    """
    while True:
        try:
            trigger_event = await asyncio.wait_for(queue.get(), timeout=5.0)
        except asyncio.TimeoutError:
            continue

        try:
            finding = await agent.analyze(trigger_event)
            finding["suppressed_count"] = suppressed_ref[0]
            await on_finding(finding)
        except Exception as exc:
            logger.error("Agent worker error: %s", exc)
        finally:
            queue.task_done()
