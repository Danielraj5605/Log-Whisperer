"""Monitoring pipeline — wires buffer, trigger engine, LLM agent and alert outputs together.

Every entry point (watch, shell, run, REPL) needs the same plumbing; keeping it here means
a fix lands everywhere at once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

from collections import Counter

from agent.agent import GeminiAgent, LocalAgent
from agent.privacy import redact
from agent.storage import append_record
from agent.detect import get_project_name
from agent.session import get_telegram_config
from agent.worker import agent_worker
from buffer.ring_buffer import RingBuffer
from config import DEFAULT_CONFIG, load as load_config
from output.telegram_notification import TelegramNotifier
from output.terminal import print_alert
from output.windows_notification import WindowsNotifier
from trigger.cooldown import CooldownTracker
from trigger.engine import TriggerEngine
from trigger.rules import build_rules

logger = logging.getLogger("logwhisper")

AGENT_QUEUE_SIZE = 50

FindingHook = Callable[[dict[str, Any]], Awaitable[None] | None]


class Monitor:
    """Owns the whole log -> trigger -> LLM -> alert pipeline for one session."""

    def __init__(
        self,
        api_key: str,
        *,
        cfg: dict[str, Any] | None = None,
        model: str | None = None,
        buffer_size: int | None = None,
        windows_notifications: bool = True,
        telegram: bool = True,
        print_alerts: bool = True,
        on_finding: FindingHook | None = None,
    ) -> None:
        self.cfg = cfg if cfg is not None else load_config()
        llm_cfg = self.cfg.get("llm", {})

        self.buffer = RingBuffer(
            maxlen=buffer_size or int(self.cfg.get("buffer", {}).get("max_lines", 500))
        )
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=AGENT_QUEUE_SIZE)
        self.engine = TriggerEngine(
            rules=build_rules(self.cfg.get("triggers")),
            cooldowns=CooldownTracker(),
            agent_queue=self.queue,
        )
        self.engine.context_seconds = self.cfg.get("buffer", {}).get("context_window_seconds", 60)
        if api_key:
            self.agent = GeminiAgent(
                api_key=api_key, model=model or llm_cfg.get("model") or "gemini-2.5-flash",
                timeout=float(llm_cfg.get("timeout_seconds", 30)),
                max_tokens=int(llm_cfg.get("max_tokens", 2048)),
            )
        else:
            self.agent = LocalAgent()
        self.secret = api_key
        self.storage = self.cfg.get("storage", DEFAULT_CONFIG["storage"])
        self.sources: dict[str, str] = {}
        self.delivered: Counter[str] = Counter()
        self.completed = 0
        self.source_failures = 0
        self.delivery_failures: Counter[str] = Counter()
        self.worker: asyncio.Task | None = None

        output_cfg = self.cfg.get("output", {})
        self._print_alerts = print_alerts and bool(output_cfg.get("terminal", True))
        self._windows = (
            WindowsNotifier()
            if sys.platform == "win32" and windows_notifications and output_cfg.get("windows_notification", True)
            else None
        )
        self._telegram: TelegramNotifier | None = None
        if telegram and api_key:
            tg = get_telegram_config()
            if tg["bot_token"] and tg["chat_id"]:
                self._telegram = TelegramNotifier(bot_token=tg["bot_token"], chat_id=tg["chat_id"])

        jsonl_cfg = output_cfg.get("jsonl", {})
        self._jsonl_path: Path | None = (
            Path(jsonl_cfg.get("path") or ".logwhisper/alerts.jsonl")
            if jsonl_cfg.get("enabled", True)
            else None
        )

        self._extra_hook = on_finding
        self._project = get_project_name()
        self.finding_count = 0

    # ── Ingest ────────────────────────────────────────────────────────────────

    @property
    def suppression_count(self) -> int:
        return self.engine.suppression_count

    def ingest(self, log_obj: dict, *, live: bool = True) -> None:
        """Feed one parsed log line through the trigger engine and into the buffer.

        Live lines are windowed by *arrival* time: timestamps inside log lines can be naive
        UTC from a container, stale, or in another timezone, and must not decide whether a
        line is "recent". Batch replay (live=False) keeps the log's own timeline.
        """
        if live:
            log_obj["epoch"] = self.buffer.now()
        self.sources[log_obj["source"]] = str(log_obj["ts"])
        self.engine.process(log_obj, self.buffer)

    async def consume(self, adapter: Any) -> None:
        """Feed every line from an adapter's stream() until it ends.

        An adapter failing (missing docker binary, ...) is logged, not fatal to the others.
        """
        try:
            async for log_obj in adapter.stream():
                self.ingest(log_obj)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.source_failures += 1
            logger.error("Log source stopped: %s", exc)

    # ── Worker / outputs ──────────────────────────────────────────────────────

    def start_worker(self) -> asyncio.Task:
        """Start the background task that turns trigger events into alerts."""
        self.worker = asyncio.create_task(
            agent_worker(self.queue, self.agent, self.handle_finding)
        )

        return self.worker

    async def finish(self, timeout: float = 60) -> None:
        if self.worker:
            try:
                await asyncio.wait_for(self.queue.join(), timeout)
            except asyncio.TimeoutError:
                logger.warning("Analysis drain timed out")
            finally:
                self.worker.cancel()
                await asyncio.gather(self.worker, return_exceptions=True)
                self.worker = None
        logger.info("Session summary: %s", json.dumps(self.status()))

    @property
    def degraded(self) -> bool:
        return bool(self.source_failures or self.delivery_failures or self.engine.dropped
                    or self.completed < self.engine.accepted)

    def status(self) -> dict[str, Any]:
        return {
            "analysis": "local" if isinstance(self.agent, LocalAgent) else "gemini",
            "last_input": self.sources.copy(),
            "pending": self.engine.accepted - self.completed,
            "queued": self.queue.qsize(),
            "accepted": self.engine.accepted,
            "findings": self.finding_count,
            "suppressed": self.suppression_count,
            "dropped": self.engine.dropped,
            "source_failures": self.source_failures,
            "delivered": dict(self.delivered),
            "delivery_failures": dict(self.delivery_failures),
        }

    def capture(self, path: Path, line: str) -> None:
        if self.storage["capture_logs"]:
            try:
                append_record(path, redact(line, self.secret), self.storage)
            except (OSError, TimeoutError) as exc:
                self.delivery_failures["capture"] += 1
                logger.warning("Log capture failed: %s", exc)

    async def handle_finding(self, finding: dict[str, Any]) -> None:
        """Deliver one finding to every configured output. Never raises."""
        self.finding_count += 1
        finding["suppressed_count"] = self.engine.suppression_count

        for key, value in finding.items():
            if isinstance(value, str):
                finding[key] = redact(value, self.secret)
            elif isinstance(value, list):
                finding[key] = [redact(str(item), self.secret) for item in value]

        if self._jsonl_path:
            try:
                append_record(self._jsonl_path, json.dumps(finding, default=str), self.storage)
                self.delivered["jsonl"] += 1
            except (OSError, TimeoutError) as exc:
                self.delivery_failures["jsonl"] += 1
                logger.warning("JSONL write failed: %s", exc)

        if self._extra_hook:
            try:
                result = self._extra_hook(finding)
                if asyncio.iscoroutine(result):
                    await result
                self.delivered["hook"] += 1
            except Exception as exc:
                self.delivery_failures["hook"] += 1
                logger.warning("Finding hook failed: %s", exc)

        if self._windows:
            try:
                self._windows.notify(finding)
                self.delivered["windows"] += 1
            except Exception as exc:
                self.delivery_failures["windows"] += 1
                logger.warning("Windows notification failed: %s", exc)

        if self._telegram:
            try:
                if not await self._telegram.notify(finding, project=redact(self._project, self.secret)):
                    self.delivery_failures["telegram"] += 1
                else:
                    self.delivered["telegram"] += 1
            except Exception as exc:
                self.delivery_failures["telegram"] += 1
                logger.warning("Telegram notification failed: %s", exc)

        if self._print_alerts:
            try:
                print_alert(finding, suppressed_count=self.engine.suppression_count)
                self.delivered["terminal"] += 1
            except Exception as exc:
                self.delivery_failures["terminal"] += 1
                logger.warning("Alert display failed: %s", exc)
        self.completed += 1
