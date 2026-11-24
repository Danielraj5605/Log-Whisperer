"""Trigger engine — evaluates all rules against each log line."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from .cooldown import CooldownTracker
from .rules import BaseRule

if TYPE_CHECKING:
    from buffer.ring_buffer import RingBuffer

logger = logging.getLogger(__name__)


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
        self.dropped = 0
        self.accepted = 0
        self.context_seconds = 60

    def process(self, log_obj: dict, buffer: "RingBuffer") -> None:
        """Evaluate a new log line, THEN add it to the buffer.

        The order matters: rules such as NewErrorTypeRule ask the buffer whether a
        signature has been seen *before* this line, so pushing first would make every
        error look like a repeat.
        """
        try:
            self.evaluate(log_obj, buffer)
        finally:
            buffer.push(log_obj)

    def evaluate(self, log_obj: dict, buffer: "RingBuffer") -> None:
        """Evaluate all rules. Fire ONE event to the agent queue per line, unless cooling.

        When several rules match the same line they are merged into a single event
        (rule names joined with " + ") so the LLM is asked about the line only once.
        """
        source = log_obj["source"]
        fired: list[BaseRule] = []

        for rule in self.rules:
            try:
                matched = rule.evaluate(log_obj, buffer)
            except Exception:
                logger.exception("Rule %s failed on line from %s", rule.name, source)
                continue
            if not matched:
                continue

            if self.cooldowns.is_cooling(rule.name, source, rule.cooldown):
                self.suppression_count += 1
                continue
            fired.append(rule)

        if not fired:
            return

        # The triggering line is not in the buffer yet — append it so the LLM sees it
        window = buffer.window(seconds=self.context_seconds)
        window.append(log_obj)

        trigger_event = {
            "rule": " + ".join(rule.name for rule in fired),
            "rules": [rule.name for rule in fired],
            "log_obj": log_obj,
            "buffer_window": window,
            "stats": buffer.stats(source),
        }

        try:
            self.agent_queue.put_nowait(trigger_event)
        except asyncio.QueueFull:
            # Don't start the cooldowns: the event was dropped, so the next matching
            # line should get another chance to be analyzed.
            self.dropped += 1
            return

        self.accepted += 1
        for rule in fired:
            self.cooldowns.record(rule.name, source)
