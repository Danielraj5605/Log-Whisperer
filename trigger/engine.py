"""Trigger engine — evaluates all rules against each log line."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from .cooldown import CooldownTracker
from .rules import BaseRule

if TYPE_CHECKING:
    from buffer.ring_buffer import RingBuffer


class TriggerEngine:
    """Evaluates log lines against rules, pushes events to agent queue on fire."""

    def __init__(
        self,
        rules: list[BaseRule],
        cooldowns: CooldownTracker,
        agent_queue: asyncio.Queue[dict[str, Any]],
    ) -> None:
        self.rules = rules
        self.cooldowns = cooldowns
        self.agent_queue = agent_queue
        self.suppression_count = 0

    def evaluate(self, log_obj: dict, buffer: "RingBuffer") -> None:
        """Evaluate all rules. Fire to agent queue if not in cooldown."""
        for rule in self.rules:
            if not rule.evaluate(log_obj, buffer):
                continue

            rule_name = rule.name
            source = log_obj["source"]

            if self.cooldowns.is_cooling(rule_name, source, rule.cooldown):
                self.suppression_count += 1
                continue

            self.cooldowns.record(rule_name, source)

            trigger_event = {
                "rule": rule_name,
                "log_obj": log_obj,
                "buffer_window": buffer.window(seconds=60),
                "stats": buffer.stats(source),
            }

            try:
                self.agent_queue.put_nowait(trigger_event)
            except asyncio.QueueFull:
                self.suppression_count += 1  # Count queue overflow as suppressed too
