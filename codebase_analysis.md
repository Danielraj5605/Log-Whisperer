# Log Whisperer — Codebase Analysis

> Generated: 2026-06-08 | Model: Claude Sonnet 4.6 (Thinking)

---

## Table of Contents

1. [What Is It?](#what-is-it)
2. [Architecture Overview](#architecture-overview)
3. [Module-by-Module Breakdown](#module-by-module-breakdown)
4. [Full Data Flow (End to End)](#full-data-flow-end-to-end)
5. [CLI Commands](#cli-commands)
6. [Key Design Decisions](#key-design-decisions)
7. [Drawbacks](#drawbacks)
8. [Incomplete / Stubbed Functions](#incomplete--stubbed-functions)

---

## What Is It?

**Log Whisperer** (`logwhisper`) is an **AI-powered live log intelligence agent** that monitors your development server logs in real-time, detects anomalies using rule-based triggers, and sends those anomalies to an LLM (MiniMax or Google Gemini) for intelligent root-cause analysis — all inside your terminal.

Think of it as a **smart `tail -f`** with an AI brain that tells you *why* errors are happening, not just *what* they say.

---

## Architecture Overview

```
┌────────────────────────────────────────────────────────────────┐
│                         CLI Entry Point                         │
│                    agent/__init__.py  (Typer)                   │
│  Commands: run | shell | watch | chat | setup | telegram        │
└────────────────────┬───────────────────────────────────────────┘
                     │
        ┌────────────▼────────────┐
        │   REPL / Run Session   │
        │  agent/repl.py         │  ← interactive shell
        │  agent/run.py          │  ← legacy auto-run flow
        └────┬────────┬──────────┘
             │        │
     ┌───────▼───┐  ┌─▼───────────────────┐
     │  Ingest   │  │   Project Detection  │
     │           │  │   agent/detect.py    │
     │ file_     │  │  - Node/Python       │
     │ adapter   │  │  - frontend/backend  │
     │ (tail -f) │  │  - npm/pip commands  │
     └────┬──────┘  └─────────────────────┘
          │
     ┌────▼──────────────────────────┐
     │         buffer/               │
     │  parser.py  — normalize line  │
     │  ring_buffer.py — sliding win  │
     │  - 500-line deque             │
     │  - signature tracking         │
     │  - error rate (10s buckets)   │
     └────┬──────────────────────────┘
          │
     ┌────▼──────────────────────────────────────────┐
     │              trigger/                          │
     │  engine.py — evaluates all rules per log line │
     │  cooldown.py — per (rule, source) suppression │
     │  rules.py — rule classes:                     │
     │    NewErrorTypeRule    — new error fingerprint │
     │    RateSpikeRule       — ≥3 errors in 10s     │
     │    CriticalKeywordRule — fatal/oom/panic/etc  │
     │    HttpErrorRule       — 4xx/5xx in log line  │
     │    MetricThresholdRule — latency/cpu/mem       │
     │    ClusterFormationRule — errors across svcs  │
     │    SilenceAnomalyRule  — source goes silent   │
     └────┬──────────────────────────────────────────┘
          │ trigger_event (rule, log_obj, buffer_window, stats)
     ┌────▼──────────────────────────────┐
     │   asyncio.Queue (max 50)          │
     └────┬──────────────────────────────┘
          │
     ┌────▼──────────────────────────────────────────┐
     │         agent/worker.py  (background task)    │
     │   Consumes queue → calls agent.analyze()      │
     └────┬──────────────────────────────────────────┘
          │
     ┌────▼──────────────────────────────────────────┐
     │         agent/agent.py  — MiniMaxAgent         │
     │   Sends last 100 log lines + trigger stats    │
     │   to MiniMax LLM via OpenAI-compatible API    │
     │   Returns structured JSON "finding":          │
     │   { root_signal, caused_by, confidence,       │
     │     contributing_factors, blast_radius,       │
     │     evidence, action, kb_used }               │
     └────┬──────────────────────────────────────────┘
          │ finding dict
     ┌────▼──────────────────────────────────────────┐
     │              output/                           │
     │  terminal.py         — Rich panel alert UI    │
     │  windows_notification.py — Toast (winotify)  │
     │  telegram_notification.py — Bot API message  │
     │  dashboard.py        — Live service dashboard  │
     └───────────────────────────────────────────────┘
```

---

## Module-by-Module Breakdown

| Module | File(s) | Role |
|---|---|---|
| **CLI** | `agent/__init__.py` | Typer CLI, all commands wired here |
| **REPL** | `agent/repl.py` | Interactive shell (`/run`, `/chat`, `/watch`, `/stop`, etc.) |
| **Legacy Run** | `agent/run.py` | Auto-detect → spawn services → monitor, older orchestration path |
| **LLM Agent** | `agent/agent.py` | MiniMaxAgent: formats context, calls MiniMax API, parses JSON finding |
| **Chat Agent** | `agent/chat.py` | Conversational mode supporting MiniMax + Gemini, with auto-fallback |
| **Session** | `agent/session.py` | API key & Telegram creds stored in `%APPDATA%\logwhisper\credentials.json` |
| **Detection** | `agent/detect.py` | Auto-detects Node/Python projects, finds `npm run dev` / `python main.py` |
| **Worker** | `agent/worker.py` | Background async loop: queue → LLM → callback |
| **Parser** | `buffer/parser.py` | Normalizes raw log lines: extracts timestamp, level, fields, SHA1 signature |
| **Ring Buffer** | `buffer/ring_buffer.py` | 500-line deque, tracks error rate by 10s buckets, signature deduplication |
| **File Adapter** | `ingest/file_adapter.py` | `tail -f` style async file watcher, handles log rotation via inode check |
| **Trigger Engine** | `trigger/engine.py` | Runs all rules per log line, pushes trigger events to queue if not cooling |
| **Rules** | `trigger/rules.py` | 7 trigger rule classes with cooldowns (5–300s) |
| **Cooldown** | `trigger/cooldown.py` | Per `(rule, source)` suppression timer |
| **Terminal** | `output/terminal.py` | Rich panels for alerts, live status bar, service log stream |
| **Telegram** | `output/telegram_notification.py` | Formats + sends HTML alert messages to Telegram Bot API |
| **Windows** | `output/windows_notification.py` | Windows toast notifications via `winotify` |
| **Config** | `config.py` | Loads `.logwhisper.yaml`, merges with defaults |

---

## Full Data Flow (End to End)

```
1. User runs: logwhisper watch --file ./logs/app.log
2. FileAdapter tails the file every 100ms
3. Each new line → parser.parse() → normalized log_obj
4. log_obj pushed to RingBuffer (sliding 500-line window)
5. TriggerEngine.evaluate() checks all 4 active rules:
   - NewErrorTypeRule    → new SHA1 fingerprint?
   - RateSpikeRule       → ≥3 errors in last 10s?
   - CriticalKeywordRule → "fatal", "oom", "panic"...?
   - HttpErrorRule       → HTTP 4xx/5xx in line?
6. If rule fires AND not in cooldown:
   → trigger_event = {rule, log_obj, buffer_window(60s), stats}
   → put into asyncio.Queue
7. agent_worker (background) picks it up:
   → MiniMaxAgent.analyze(trigger_event)
   → Sends system prompt + last 100 log lines to MiniMax API
   → Returns structured JSON "finding"
8. on_finding() callback fires:
   → print_alert() → Rich panel in terminal
   → WindowsNotifier → Windows toast pop-up
   → TelegramNotifier → Telegram message to your phone
```

---

## CLI Commands

| Command | Description |
|---|---|
| `logwhisper` | Launch interactive REPL (no args) |
| `logwhisper run` | Auto-detect project, start all services, monitor |
| `logwhisper watch --file ./app.log` | Watch a specific file with AI alerts |
| `logwhisper shell` | Combined shell + log monitor |
| `logwhisper chat --error "..."` | One-shot AI error analysis |
| `logwhisper chat` | Interactive chat REPL with the AI |
| `logwhisper setup` | First-time wizard (API key + Telegram) |
| `logwhisper telegram status/test/clear` | Manage Telegram config |
| `logwhisper project ./a ./b` | Launch multiple projects in separate windows |

---

## Key Design Decisions

1. **Signature-based deduplication** — Each log line gets a SHA1 fingerprint (`level:first80chars`) so the same error doesn't trigger the LLM repeatedly.
2. **Cooldown suppression** — Per `(rule, source)` cooldowns (5–300s depending on rule) prevent alert storms.
3. **Dual LLM support** — Primary is MiniMax, fallback is Google Gemini — automatically fails over on auth errors or rate limits.
4. **Credentials stored globally** — In `%APPDATA%\logwhisper\credentials.json` (Windows) so they persist across projects.
5. **ANSI stripping** — Service output is cleaned of terminal color codes before feeding to the trigger engine.
6. **Log rotation awareness** — FileAdapter detects inode changes to handle rotated log files gracefully.

---

## Drawbacks

### 1. Single-threaded LLM queue — no concurrency

In `agent/worker.py`, only **one** trigger event is processed at a time. If MiniMax takes 10–30s per call (its timeout), the queue backs up silently. With `maxsize=50`, events just get dropped and counted as "suppressed" — losing real alerts.

### 2. RingBuffer signature set grows unboundedly

In `buffer/ring_buffer.py`, `self._signatures: set[str]` is **never pruned**. It grows forever for the lifetime of the process. A long-running session will eventually treat every error as "already seen" or eat memory silently.

### 3. `error_rate()` is misleading — counts all lines, not just errors

In `buffer/ring_buffer.py`, `_freq` buckets count **every pushed log line**, not just error lines. So `RateSpikeRule` fires based on total log volume, not actual error rate — producing false positives in verbose services.

### 4. `window()` uses string-comparison timestamps

In `buffer/ring_buffer.py`, `l["ts"] >= cutoff_ts` compares ISO 8601 strings lexicographically. This works only if timestamps are in UTC with the same format — logs with different timezones or formats that parsed differently will break silently.

### 5. `FileAdapter` silently seeks to EOF on first open

In `ingest/file_adapter.py`, on first start it always seeks to the **end** of the file. This means if you point it at an existing log with prior errors, those are completely ignored. There's no `--from-beginning` flag or backfill option.

### 6. Parser level detection is too simple — first line bias

In `buffer/parser.py`, `_LEVEL_PATTERN` only matches at the **start of the line** (`^`). Multi-line exceptions, stack traces, or log formats where the level appears mid-line (e.g., Flask, Django, Gunicorn output) get defaulted to `INFO` — causing `NewErrorTypeRule`'s level check to miss them, relying only on the raw keyword fallback.

### 7. No persistence across restarts

The alert history, suppression counts, and the `_signatures` deduplication set all live in memory only. Restarting the agent means it will re-alert on every previously-seen error fingerprint.

### 8. `deps.py` dependency checking is fragile

`agent/deps.py` is used in `run.py` and `repl.py` but the actual install command runs via `subprocess.run(..., shell=True)` with no sandboxing. Also `check_service_deps()` returning `missing=True` is based on heuristics (presence of lock files), not actual import verification.

### 9. Telegram & Windows notifications are synchronous / blocking

In `output/telegram_notification.py` and `output/windows_notification.py`, both use **synchronous** `httpx.post()` and `winotify.show()` inside `on_finding()` which runs in the async event loop. This **blocks** the entire loop until the HTTP call completes or times out (10s for Telegram).

### 10. No actual Gemini support in the monitoring agent

`agent/chat.py` (interactive chat) supports both MiniMax and Gemini. But `agent/agent.py` (the live monitoring agent) is **MiniMax-only** — hardcoded to `https://api.minimax.chat/v1`. The `--provider` flag on `watch`/`shell` commands doesn't actually route to Gemini for live alerts.

### 11. `_freq` dict in RingBuffer is never cleaned up

In `buffer/ring_buffer.py`, `self._freq` grows a new bucket every 10 seconds per source, forever. Old buckets are never evicted — another memory leak in long-running sessions.

### 12. Multi-project `project` command is Windows-only tested

In `agent/__init__.py`, the Linux path tries `gnome-terminal` then falls back to `xterm` — no support for macOS Terminal, iTerm, Warp, tmux, or any other common terminal.

---

## Incomplete / Stubbed Functions

| Location | What's Stubbed | Evidence |
|---|---|---|
| `agent/__init__.py` — `analyze` command | **Batch log analysis** | `"Batch analysis not yet implemented"` + `raise typer.Exit(1)` |
| `agent/__init__.py` — `watch --docker` flag | **Docker container watching** | Comment: `"Docker adapter is stubbed for now"` — the `--docker` flag exists in the CLI but does nothing |
| `trigger/rules.py` — `MetricThresholdRule` | **Metric threshold alerts** | Implemented but **never registered** in any active rule list in `run.py`, `repl.py`, or `__init__.py` |
| `trigger/rules.py` — `ClusterFormationRule` | **Cross-service error clustering** | Implemented but **never registered** anywhere |
| `trigger/rules.py` — `SilenceAnomalyRule` | **Source silence detection** | Implemented but **never registered** anywhere |
| `output/dashboard.py` | **Live dashboard** | File exists and is used, but has no graceful terminal resize handling and no `--no-dashboard` flag exposed in CLI |
| `agent/agent.py` — `kb_used` field | **Knowledge Base feature** | `"kb_used": false` is hardcoded in all fallback findings — a KB system was planned but never built |
| `output/terminal.py` — `kb_match` in alert panel | **KB match display** | `kb_match` is rendered in the alert UI but no code ever populates this field in any finding dict |
| `config.py` — trigger thresholds | **Config-driven rule tuning** | `rate_spike`, `new_error_type`, `critical_keyword` config values are loaded but the active rule constructors in every command are **hardcoded** and ignore the config |
| `config.py` — `webhook` output | **Slack / Webhook notifications** | `webhook: {enabled: False, url: "", format: "slack"}` is in the config schema but no `WebhookNotifier` class exists anywhere |
| `config.py` — `jsonl` output | **JSONL alert log file** | `jsonl: {enabled: True, path: ".logwhisper/alerts.jsonl"}` is configured but no code ever writes alerts to this file |

---

### Summary — The Biggest Issues

| Priority | Issue |
|---|---|
| 🔴 High | **3 trigger rules are dead code** — `MetricThresholdRule`, `ClusterFormationRule`, `SilenceAnomalyRule` are written but never plugged in |
| 🔴 High | **Config values are ignored** — YAML thresholds, cooldowns, and enabled flags don't flow into the rule instances |
| 🔴 High | **Blocking I/O in async context** — Telegram HTTP calls block the entire event loop |
| 🟡 Medium | **Docker support is fake** — the `--docker` flag is wired in the CLI but the adapter was never built |
| 🟡 Medium | **Memory leaks** — `_signatures` set and `_freq` dict grow forever with no eviction |
| 🟡 Medium | **No Gemini in live monitoring** — only the chat command uses Gemini; the alert agent is MiniMax-only |
| 🟢 Low | **No persistence** — deduplication state resets on every restart |
| 🟢 Low | **No backfill** — FileAdapter always starts from end of file |
| 🟢 Low | **String timestamp comparisons** — fragile across log format variations |
