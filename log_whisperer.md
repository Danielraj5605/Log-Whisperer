# Log Whisperer
### AI-Powered Live Log Intelligence Agent for Developers

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Problem Statement](#2-problem-statement)
3. [Solution](#3-solution)
4. [System Architecture](#4-system-architecture)
5. [Layer 1 — Stream Ingester](#5-layer-1--stream-ingester)
6. [Layer 2 — Ring Buffer & Parser](#6-layer-2--ring-buffer--parser)
7. [Layer 3 — Trigger Engine](#7-layer-3--trigger-engine)
8. [Layer 4 — AI Agent](#8-layer-4--ai-agent)
9. [Layer 5 — Output & Alert Routing](#9-layer-5--output--alert-routing)
10. [CLI Reference](#10-cli-reference)
11. [Configuration](#11-configuration)
12. [Tech Stack](#12-tech-stack)
13. [Project File Structure](#13-project-file-structure)
14. [Build Order](#14-build-order)
15. [Module Specifications](#15-module-specifications)
16. [Data Schemas](#16-data-schemas)
17. [Agent Prompt Design](#17-agent-prompt-design)
18. [Knowledge Base Design](#18-knowledge-base-design)
19. [Alert Format Specification](#19-alert-format-specification)
20. [Extension Points](#20-extension-points)
21. [Evaluation Benchmarks](#21-evaluation-benchmarks)
22. [Bonus Features](#22-bonus-features)

---

## 1. Project Overview

**Log Whisperer** is a command-line AI agent that watches live log streams from any source — files, Docker containers, Kubernetes pods, or piped stdin — and fires structured, evidence-backed anomaly alerts in real time.

Unlike traditional log monitoring tools that match static rules or keywords, Log Whisperer uses an LLM to reason about what is actually happening: why an error is occurring, what caused it, which downstream services are affected, and what the engineer should do right now.

| Property | Value |
|----------|-------|
| Type | CLI + AI Agent |
| Language | Python 3.11+ |
| LLM Backend | Claude / GPT-4o / Llama 3 (configurable) |
| Input | File, Docker, Kubernetes, stdin |
| Output | Terminal, Webhook, JSONL, Knowledge Base |
| Mode | Live stream (real-time) + Batch (file analysis) |
| Local-first | Yes — no cloud services required except LLM API |

---

## 2. Problem Statement

### The Pain Developers Face

When something breaks in production, developers open the terminal and stare at raw log output. The information needed to diagnose the problem is somewhere in those logs — but finding it means:

- Manually reading hundreds of lines scrolling past per second
- Grepping for known error keywords and hoping the right one is there
- Opening multiple terminal tabs to compare logs across services
- Escalating to a senior engineer when the root cause is not obvious in 30 minutes
- Writing nothing down — the debugging knowledge disappears after the fix

### Current Tooling Gaps

| Tool | What it does | What it misses |
|------|-------------|----------------|
| `grep` / `tail` | Shows matching lines | No reasoning about causes |
| Datadog / Splunk | Dashboards and alerts | Requires setup, not terminal-native, no causal reasoning |
| PagerDuty | Fires alerts on thresholds | Alerts on symptoms, not root cause |
| ELK Stack | Log aggregation and search | Requires infrastructure, passive not proactive |
| Static linters | Checks for known patterns | Cannot reason about novel failure patterns |

### Business Impact of the Current State

- Average time-to-diagnose per incident: **1.5 to 3 hours**
- Developer context switches per debugging session: **6 to 10** (editor, browser, terminal, Slack, docs)
- Knowledge retained after a debugging session: **near zero** — no structured capture
- Bugs that recur within 90 days because no root cause was documented: **~40%**

---

## 3. Solution

Log Whisperer inserts an AI reasoning layer between the raw log stream and the developer's eyes.

### Core Idea

```
Raw logs  →  [Smart trigger]  →  [AI agent]  →  Structured alert
```

The agent does not run on every log line — that would be too slow and expensive. Instead, a lightweight trigger engine evaluates every line in microseconds and only wakes the AI when something is worth reasoning about: a new error type, a rate spike, a critical keyword, or a metric threshold crossing.

When the agent runs, it receives a context window of recent logs and reasons about:

1. What is the **root signal** — the one event most likely causing everything else
2. What are the **contributing factors** — earlier warning signs
3. What is the **blast radius** — which services or users are affected
4. What is the **suggested action** — one concrete thing to do right now
5. Has this happened before — **KB match** from past resolved incidents

### What the Engineer Sees

```
⚠  ANOMALY DETECTED  [08:14:52 → 08:15:03]
Trigger     : rate_spike — 47 errors in 11s (auth-service)
Root signal : TypeError: Cannot read properties of null (session.js:142)
Caused by   : Redis cache miss on cold start → null session passed to getUser()
Evidence    : 08:14:47 WARN redis: cache miss for key session:u_891
Blast radius: 3 downstream services — checkout, profile, notifications
Action      : Check REDIS_TTL env var — likely set to 0 in this environment
KB match    : Similar to incident 2025-02-11 (resolved: set TTL=3600)
Suppressed  : 41 duplicate alerts during cooldown window
```

---

## 4. System Architecture

### End-to-End Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                         LOG SOURCES                              │
│    File │ Docker container │ Kubernetes pod │ stdin pipe         │
└─────────────────────┬───────────────────────────────────────────┘
                       │  raw log lines (text)
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                    STREAM INGESTER                               │
│  Source adapters → normalize → asyncio queue (unified stream)   │
└─────────────────────┬───────────────────────────────────────────┘
                       │  normalized log objects
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│              RING BUFFER + PARSER                                │
│  deque(maxlen=500) │ parser │ frequency counters │ sig set       │
└─────────────────────┬───────────────────────────────────────────┘
                       │  every line, synchronously
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                   TRIGGER ENGINE                                 │
│  Rule evaluation (microseconds) │ cooldown check                 │
│  ┌─────────┐  ┌──────────┐  ┌──────────┐  ┌────────────────┐  │
│  │new error│  │rate spike│  │keyword   │  │metric threshold│  │
│  └─────────┘  └──────────┘  └──────────┘  └────────────────┘  │
└──────────────┬──────────────────────────────────────────────────┘
               │  trigger fires → background worker
               ▼
┌─────────────────────────────────────────────────────────────────┐
│                      AI AGENT                                    │
│  context assembly → KB search → LLM call → finding              │
│  (runs async — log stream never blocks)                          │
└──────────────┬──────────────────────────────────────────────────┘
               │  structured finding
               ▼
┌─────────────────────────────────────────────────────────────────┐
│                   OUTPUT LAYER                                   │
│  Terminal (rich) │ Webhook │ JSONL store │ KB write              │
└─────────────────────────────────────────────────────────────────┘
```

### Key Design Principles

- **Non-blocking** — stream ingestion, trigger evaluation, and agent reasoning run in separate async workers. The log tail never pauses.
- **Agent on demand** — the LLM is called only when a trigger fires. Cost is proportional to anomaly rate, not log volume.
- **Cooldown-aware** — each trigger type per source has an independent cooldown window. 200 identical errors produce one alert.
- **Local-first** — no cloud infrastructure required. Only the LLM API call is external. Everything else runs on the developer's machine.
- **Multi-source** — all adapters emit to the same queue. The buffer, trigger, and agent see a unified stream regardless of source.

---

## 5. Layer 1 — Stream Ingester

### Responsibility

Connect to one or more log sources and emit a normalized log object for every line into a shared asyncio queue.

### Source Adapters

#### File Tail Adapter — `ingest/file_adapter.py`

- Uses a `seek()` loop to tail a file without loading it into memory
- Detects log rotation (file shrinks or inode changes) and re-opens
- Configurable polling interval (default: 100ms)

```python
# Usage
adapter = FileAdapter(path="./logs/app.log", poll_ms=100)
async for log_obj in adapter.stream():
    await queue.put(log_obj)
```

#### Docker Adapter — `ingest/docker_adapter.py`

- Calls `docker logs -f --since 0s <container>` via subprocess
- Alternatively uses Docker Python SDK for richer metadata
- Accepts container name or container ID

```python
adapter = DockerAdapter(container="auth-service")
```

#### Kubernetes Adapter — `ingest/k8s_adapter.py`

- Uses `kubectl logs -f <pod>` via subprocess
- Or Kubernetes Python client `read_namespaced_pod_log` with `follow=True`
- Supports namespace and label selectors

```python
adapter = K8sAdapter(pod="payment-pod-7d9f", namespace="prod")
```

#### Stdin Adapter — `ingest/stdin_adapter.py`

- Reads from `sys.stdin` line by line
- Enables: `npm start | logwhisper watch`
- Works with any tool that writes to stdout

#### Abstract Base — `ingest/base.py`

All adapters implement the same interface:

```python
class BaseAdapter(ABC):
    @abstractmethod
    async def stream(self) -> AsyncIterator[dict]:
        """Yield normalized log objects indefinitely."""
        ...
```

### Multi-Source Multiplexing

When multiple sources are active, all adapters run concurrently as async tasks and push into the same asyncio queue:

```python
# Each adapter is an independent task
tasks = [asyncio.create_task(adapter.stream()) for adapter in adapters]
```

### Normalized Log Object

Every adapter outputs the same structure regardless of source:

```json
{
  "ts": "2025-03-15T08:14:52.341Z",
  "source": "auth-service",
  "level": "ERROR",
  "raw": "TypeError: Cannot read properties of null (reading 'userId')",
  "fields": {
    "file": "session.js",
    "line": 142,
    "latency_ms": null
  },
  "signature": "a3f9d2c8",
  "adapter": "docker"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `ts` | ISO8601 string | Parsed timestamp from log line, or ingestion time |
| `source` | string | Container name, pod name, filename, or "stdin" |
| `level` | string | Normalized: DEBUG, INFO, WARN, ERROR, FATAL |
| `raw` | string | Original unmodified log line |
| `fields` | dict | Extracted key=value pairs from the log line |
| `signature` | string | SHA-1 of (level + first 80 chars of raw). Used for dedup. |
| `adapter` | string | Which adapter produced this object |

---

## 6. Layer 2 — Ring Buffer & Parser

### Ring Buffer — `buffer/ring_buffer.py`

A fixed-size sliding window of the most recent log objects. The AI agent reads this as its context.

```python
from collections import deque

class RingBuffer:
    def __init__(self, maxlen=500):
        self._buf = deque(maxlen=maxlen)
        self._freq = {}           # {source: {bucket_ts: error_count}}
        self._signatures = set()  # seen error signatures this session

    def push(self, log_obj: dict):
        self._buf.append(log_obj)
        self._update_freq(log_obj)
        if log_obj["level"] in ("ERROR", "FATAL"):
            self._signatures.add(log_obj["signature"])

    def window(self, seconds=60) -> list[dict]:
        """Return lines from the last N seconds."""
        cutoff = datetime.utcnow() - timedelta(seconds=seconds)
        return [l for l in self._buf if l["ts"] >= cutoff.isoformat()]

    def error_rate(self, source: str, bucket_seconds=10) -> int:
        """Return error count in the last bucket window for a source."""
        ...

    def is_new_signature(self, sig: str) -> bool:
        return sig not in self._signatures
```

### Parser — `buffer/parser.py`

Converts a raw log line string into a normalized log object.

**Timestamp parsing** — tries formats in order:

1. ISO8601 with timezone: `2025-03-15T08:14:52.341Z`
2. ISO8601 without timezone: `2025-03-15 08:14:52`
3. Epoch seconds/milliseconds: `1710492892341`
4. Common log format: `15/Mar/2025:08:14:52 +0000`
5. Fallback: current UTC time

**Level normalization:**

| Raw | Normalized |
|-----|-----------|
| CRITICAL, CRIT | FATAL |
| WARNING, WARN | WARN |
| ERROR, ERR | ERROR |
| INFO, INFORMATION | INFO |
| DEBUG, TRACE, VERBOSE | DEBUG |

**Metric extraction** — scans for `key=value` patterns:

```
latency=2847ms cpu=92% memory=95% status=500
→ fields: { "latency_ms": 2847, "cpu_pct": 92, "memory_pct": 95, "status": 500 }
```

**Error signature** — SHA-1 of `(normalized_level + ":" + raw[:80])`, lowercased.

---

## 7. Layer 3 — Trigger Engine

### Responsibility

Evaluate every incoming log line against a set of rules. Rules run in microseconds. When a rule fires, push a trigger event to the agent queue — unless the cooldown for that trigger type is active.

### Trigger Rules — `trigger/rules.py`

#### Rule 1: New Error Type

```python
class NewErrorTypeRule:
    """Fires when an error signature appears for the first time this session."""
    cooldown = 300

    def evaluate(self, log_obj, buffer) -> bool:
        if log_obj["level"] not in ("ERROR", "FATAL"):
            return False
        return buffer.is_new_signature(log_obj["signature"])
```

#### Rule 2: Rate Spike

```python
class RateSpikeRule:
    cooldown = 60

    def __init__(self, threshold=5, window_seconds=10):
        self.threshold = threshold
        self.window_seconds = window_seconds

    def evaluate(self, log_obj, buffer) -> bool:
        rate = buffer.error_rate(log_obj["source"], self.window_seconds)
        return rate >= self.threshold
```

- Default: 5 errors in 10 seconds from the same source

#### Rule 3: Critical Keyword

```python
CRITICAL_KEYWORDS = [
    "fatal", "oom", "out of memory", "segfault", "segmentation fault",
    "panic", "kernel panic", "connection refused", "disk full",
    "no space left", "deadlock", "stack overflow", "core dumped"
]

class CriticalKeywordRule:
    cooldown = 120

    def evaluate(self, log_obj, buffer) -> bool:
        raw_lower = log_obj["raw"].lower()
        return any(kw in raw_lower for kw in CRITICAL_KEYWORDS)
```

#### Rule 4: Metric Threshold

```python
class MetricThresholdRule:
    cooldown = 60

    def __init__(self, thresholds: dict):
        # e.g. {"latency_ms": 2000, "cpu_pct": 90, "memory_pct": 85}
        self.thresholds = thresholds

    def evaluate(self, log_obj, buffer) -> bool:
        fields = log_obj.get("fields", {})
        for metric, limit in self.thresholds.items():
            if fields.get(metric, 0) > limit:
                return True
        return False
```

#### Rule 5: Cluster Formation

```python
class ClusterFormationRule:
    cooldown = 120

    def __init__(self, min_sources=3, window_seconds=30):
        self.min_sources = min_sources
        self.window_seconds = window_seconds

    def evaluate(self, log_obj, buffer) -> bool:
        recent = buffer.window(seconds=self.window_seconds)
        sources_with_errors = {
            l["source"] for l in recent
            if l["level"] in ("ERROR", "FATAL")
        }
        return len(sources_with_errors) >= self.min_sources
```

#### Rule 6: Silence Anomaly

```python
class SilenceAnomalyRule:
    """Fires when an active source stops emitting for too long."""
    cooldown = 300

    def evaluate(self, log_obj, buffer) -> bool:
        # checks last-seen timestamp per source against silence threshold
        ...
```

### Cooldown Tracker — `trigger/cooldown.py`

Each (rule_name, source) pair has an independent cooldown:

```python
class CooldownTracker:
    def __init__(self):
        self._last_fired = {}  # {(rule, source): datetime}

    def is_cooling(self, rule: str, source: str, cooldown_seconds: int) -> bool:
        key = (rule, source)
        last = self._last_fired.get(key)
        if last is None:
            return False
        return (datetime.utcnow() - last).seconds < cooldown_seconds

    def record(self, rule: str, source: str):
        self._last_fired[(rule, source)] = datetime.utcnow()
```

Default cooldowns per rule:

| Rule | Default Cooldown |
|------|-----------------|
| new_error_type | 300s (5 min) |
| rate_spike | 60s |
| critical_keyword | 120s |
| metric_threshold | 60s |
| cluster_formation | 120s |
| silence_anomaly | 300s |

### Trigger Engine — `trigger/engine.py`

```python
class TriggerEngine:
    def __init__(self, rules, cooldowns, agent_queue):
        self.rules = rules
        self.cooldowns = cooldowns
        self.agent_queue = agent_queue
        self.suppression_count = 0

    def evaluate(self, log_obj: dict, buffer: RingBuffer):
        for rule in self.rules:
            if rule.evaluate(log_obj, buffer):
                name = rule.__class__.__name__
                source = log_obj["source"]
                if self.cooldowns.is_cooling(name, source, rule.cooldown):
                    self.suppression_count += 1
                    continue
                self.cooldowns.record(name, source)
                trigger_event = {
                    "rule": name,
                    "log_obj": log_obj,
                    "buffer_window": buffer.window(seconds=60),
                    "stats": buffer.stats(source),
                }
                self.agent_queue.put_nowait(trigger_event)
```

---

## 8. Layer 4 — AI Agent

### Responsibility

Receive a trigger event, assemble a context packet, optionally search the knowledge base, call the LLM, and return a structured finding. Runs asynchronously — the log stream never waits.

### Context Assembly — `agent/context.py`

The context packet sent to the LLM:

```json
{
  "trigger": {
    "rule": "RateSpikeRule",
    "source": "auth-service",
    "fired_at": "2025-03-15T08:15:03Z"
  },
  "window": [
    "...last 60 seconds of buffer, max 500 lines, as formatted strings..."
  ],
  "stats": {
    "errors_per_10s": 47,
    "unique_error_types": 3,
    "total_lines_in_window": 312
  },
  "session_alerts": [
    "...last 3 alerts fired this session, as summaries..."
  ],
  "kb_matches": [
    {
      "title": "Redis null on cold start",
      "date": "2025-02-11",
      "root_cause": "REDIS_TTL=0 caused cache misses on every request",
      "resolution": "Set REDIS_TTL=3600 in .env",
      "similarity": 0.91
    }
  ]
}
```

**Context window strategy:**

- Start with the last 60 seconds of buffer (most relevant)
- If under 100 lines, expand to 120 seconds
- If over 500 lines, sample: keep all ERROR/FATAL lines + every 5th INFO line
- Prepend a 2-sentence rolling summary of the last 10 minutes for broader context

### Knowledge Base Search — `agent/kb.py`

```python
class KnowledgeBase:
    def __init__(self, path=".logwhisper/kb"):
        self.client = chromadb.PersistentClient(path=path)
        self.collection = self.client.get_or_create_collection("incidents")
        self.embedder = SentenceTransformer("all-MiniLM-L6-v2")

    def search(self, query: str, top_k=3) -> list[dict]:
        embedding = self.embedder.encode(query).tolist()
        results = self.collection.query(
            query_embeddings=[embedding],
            n_results=top_k
        )
        return results["metadatas"][0]

    def store(self, alert: dict):
        text = f"{alert['root_signal']} {alert['caused_by']} {alert['action']}"
        embedding = self.embedder.encode(text).tolist()
        self.collection.add(
            ids=[alert["id"]],
            embeddings=[embedding],
            metadatas=[alert],
            documents=[text]
        )
```

### LLM Call — `agent/agent.py`

```python
class Agent:
    def __init__(self, client, kb: KnowledgeBase):
        self.client = client
        self.kb = kb

    async def analyze(self, trigger_event: dict) -> dict:
        # 1. Search KB
        query = trigger_event["log_obj"]["raw"]
        kb_matches = self.kb.search(query)

        # 2. Assemble context
        ctx = assemble_context(trigger_event, kb_matches)

        # 3. LLM call
        response = await self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": format_context(ctx)}]
        )

        # 4. Parse structured response
        finding = parse_finding(response.content[0].text)

        # 5. Store alert in KB
        self.kb.store(finding)

        return finding
```

### Prompt Templates — `agent/prompts.py`

#### System Prompt

```
You are Log Whisperer, a live log analysis AI agent embedded in a developer's terminal.

Your job: given a window of recent log lines and a trigger event, reason about what is
actually happening — not just what the error says, but why it is occurring and what it means.

Always respond in this exact JSON structure:
{
  "root_signal": "The single most important log line or event",
  "caused_by": "Plain English explanation of root cause",
  "confidence": "high | medium | low",
  "contributing_factors": ["factor 1", "factor 2"],
  "blast_radius": "Which services or users are affected",
  "evidence": ["timestamp: log line", "timestamp: log line"],
  "action": "One concrete step the engineer should take right now",
  "kb_used": true | false
}

Rules:
- Never invent log lines. Only cite lines from the provided window.
- If you are uncertain, set confidence to "low" and explain why.
- Keep caused_by under 2 sentences.
- Keep action specific and immediately actionable.
- Do not repeat the raw error message in caused_by — explain the cause behind it.
```

#### User Message Format

```
TRIGGER: {rule_name} fired on source "{source}" at {timestamp}

STATS:
- Errors in last 10s: {errors_per_10s}
- Unique error types: {unique_error_types}
- Lines in window: {total_lines}

RECENT LOG WINDOW (last 60 seconds):
{formatted_log_lines}

PAST SIMILAR INCIDENTS:
{kb_matches_formatted}

SESSION CONTEXT (earlier this watch session):
{session_alerts_summary}

Analyze this situation and return your finding as JSON.
```

---

## 9. Layer 5 — Output & Alert Routing

### Terminal Output — `output/terminal.py`

Uses the `rich` library for color-coded, structured display. The log stream continues scrolling above the alert panel.

**Alert panel format:**

```
╔══════════════════════════════════════════════════════════╗
║  ⚠  ANOMALY DETECTED   08:14:52 → 08:15:03              ║
╠══════════════════════════════════════════════════════════╣
║  Trigger       rate_spike — 47 errors/11s (auth-service) ║
║  Root signal   TypeError: null session (session.js:142)  ║
║  Caused by     Redis cache miss on cold start →          ║
║                null session passed to getUser()          ║
║  Evidence      08:14:47 WARN redis: cache miss u_891     ║
║  Blast radius  checkout, profile, notifications (3)      ║
║  Action        Check REDIS_TTL env var — likely 0        ║
║  KB match      2025-02-11 — resolved: set TTL=3600       ║
║  Suppressed    41 duplicate alerts during cooldown       ║
╚══════════════════════════════════════════════════════════╝
```

**Color coding:**

| Severity | Color | When |
|----------|-------|------|
| CRITICAL | Red bold | Confidence high + FATAL trigger |
| WARNING | Yellow | Confidence medium or ERROR trigger |
| INFO | Cyan | Confidence low or informational |

**Live status bar** (bottom of terminal, always visible):

```
● Watching: auth-service, checkout-api, payment-service
  Alerts: 3  │  Suppressed: 41  │  KB entries: 128  │  Uptime: 00:42:17
```

### Webhook Output — `output/webhook.py`

POST JSON payload to any configured endpoint:

```json
{
  "alert_id": "LW-2025-0892",
  "timestamp": "2025-03-15T08:15:05Z",
  "source": "auth-service",
  "trigger": "rate_spike",
  "confidence": "high",
  "root_signal": "TypeError: null session (session.js:142)",
  "caused_by": "Redis cache miss on cold start → null session passed to getUser()",
  "blast_radius": "checkout, profile, notifications",
  "action": "Check REDIS_TTL env var — likely set to 0 in this environment",
  "evidence": [
    "2025-03-15T08:14:47Z WARN redis: cache miss for key session:u_891"
  ],
  "kb_match": {
    "title": "Redis null on cold start",
    "date": "2025-02-11",
    "similarity": 0.91
  }
}
```

Compatible with: Slack Incoming Webhooks, PagerDuty Events API v2, OpsGenie, custom receivers.

### JSONL Alert Store — `output/store.py`

Every alert appended to `.logwhisper/alerts.jsonl`:

- Append-only, one JSON object per line
- Trivially parseable by `jq`, Python, or any dashboard tool
- Used for post-session analysis

---

## 10. CLI Reference

### Installation

```bash
pip install logwhisper
# or from source
git clone https://github.com/yourname/logwhisper
cd logwhisper && pip install -e .
```

### Commands

#### `logwhisper watch` — Live monitoring

```bash
logwhisper watch --file ./logs/app.log
logwhisper watch --docker auth-service
logwhisper watch --docker auth-service --docker checkout-api    # multi-source
logwhisper watch --k8s prod/payment-pod-7d9f
logwhisper watch --k8s prod --all-pods                          # entire namespace
logwhisper watch --all --namespace prod
cat access.log | logwhisper watch                               # stdin
npm start | logwhisper watch                                    # pipe from any process
```

**Flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `--file PATH` | — | Tail a log file |
| `--docker NAME` | — | Watch a Docker container |
| `--k8s POD` | — | Watch a Kubernetes pod |
| `--namespace NS` | `default` | Kubernetes namespace |
| `--all-pods` | false | Watch all pods in namespace |
| `--buffer N` | 500 | Ring buffer size (lines) |
| `--window N` | 60 | Context window for agent (seconds) |
| `--rate-threshold N` | 5 | Error rate trigger (errors/10s) |
| `--model NAME` | claude-sonnet-4-20250514 | LLM model to use |
| `--webhook URL` | — | Send alerts to this webhook |
| `--no-kb` | false | Disable knowledge base lookup |
| `--quiet` | false | Suppress status bar |

#### `logwhisper analyze` — Batch analysis

```bash
logwhisper analyze --file ./logs/incident-2025-03-15.log
logwhisper analyze --file ./logs/app.log --since "08:00" --until "09:30"
logwhisper analyze --file ./logs/app.log --format json
```

#### `logwhisper kb` — Knowledge base management

```bash
logwhisper kb list
logwhisper kb search "redis null session"
logwhisper kb show LW-2025-0892
logwhisper kb delete LW-2025-0892
logwhisper kb export --format md
```

#### `logwhisper config` — Configuration

```bash
logwhisper config init
logwhisper config show
logwhisper config set model gpt-4o
```

---

## 11. Configuration

### `.logwhisper.yaml`

```yaml
# LLM configuration
llm:
  provider: anthropic          # anthropic | openai | ollama
  model: claude-sonnet-4-20250514
  api_key: ${ANTHROPIC_API_KEY}
  max_tokens: 1000
  timeout_seconds: 30

# Ring buffer
buffer:
  max_lines: 500
  context_window_seconds: 60

# Trigger rules
triggers:
  rate_spike:
    enabled: true
    threshold: 5
    window_seconds: 10
    cooldown_seconds: 60

  new_error_type:
    enabled: true
    cooldown_seconds: 300

  critical_keyword:
    enabled: true
    cooldown_seconds: 120
    extra_keywords:
      - "checkout failed"
      - "payment timeout"
      - "auth rejected"

  metric_threshold:
    enabled: true
    cooldown_seconds: 60
    thresholds:
      latency_ms: 2000
      cpu_pct: 90
      memory_pct: 85
      error_rate: 0.05

  cluster_formation:
    enabled: true
    min_sources: 3
    window_seconds: 30
    cooldown_seconds: 120

  silence_anomaly:
    enabled: true
    silence_threshold_seconds: 60
    cooldown_seconds: 300

# Output
output:
  terminal: true
  webhook:
    enabled: false
    url: ${SLACK_WEBHOOK_URL}
    format: slack              # slack | pagerduty | raw
  jsonl:
    enabled: true
    path: .logwhisper/alerts.jsonl
  knowledge_base:
    enabled: true
    path: .logwhisper/kb
    embedding_model: all-MiniLM-L6-v2

# Log format hints
log_format:
  timestamp_field: ts
  level_field: level
  json_logs: false
```

---

## 12. Tech Stack

| Layer | Technology | Version | Why This Choice |
|-------|-----------|---------|----------------|
| Language | Python | 3.11+ | Best ecosystem for async I/O, LLM SDKs, and terminal tooling |
| CLI framework | Typer | 0.12+ | Auto-generates `--help`, type-safe flags, minimal boilerplate |
| Async runtime | asyncio | stdlib | Non-blocking multi-source tailing without threads |
| Ring buffer | collections.deque | stdlib | O(1) append + auto-eviction, zero dependencies |
| Log parsing | re + python-dateutil | 2.9+ | Handles every timestamp format edge case |
| Docker integration | docker SDK | 7.0+ | Richer metadata; subprocess fallback available |
| K8s integration | kubernetes | 28.0+ | Official Python client; subprocess fallback for simpler setups |
| AI agent | anthropic SDK | 0.30+ | Direct API call — no LangChain overhead |
| Terminal UI | rich | 13.0+ | Color panels, live status bars without ANSI fighting |
| Vector DB | chromadb | 0.5+ | In-process, no server, persists to disk |
| Embeddings | sentence-transformers | 3.0+ | Free, local, no API cost. all-MiniLM-L6-v2 |
| Config | PyYAML | 6.0+ | Per-project config with env var interpolation |
| Testing | pytest + pytest-asyncio | latest | Async-native testing for stream components |

### Alternative LLM Backends

```python
# Anthropic Claude (default)
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# OpenAI GPT-4o
client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Ollama (local, free, zero API cost)
client = openai.OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
# Supported models: llama3, mistral, deepseek-coder
```

---

## 13. Project File Structure

```
logwhisper/
│
├── cli.py                        # Typer CLI — all commands and entry points
├── config.py                     # .logwhisper.yaml loader and validator
│
├── ingest/
│   ├── __init__.py
│   ├── base.py                   # Abstract BaseAdapter interface
│   ├── file_adapter.py           # File tail with rotation detection
│   ├── docker_adapter.py         # Docker SDK / subprocess adapter
│   ├── k8s_adapter.py            # kubectl logs / k8s Python client
│   ├── stdin_adapter.py          # Piped stdin reader
│   └── multiplexer.py            # Combines multiple adapters into one queue
│
├── buffer/
│   ├── __init__.py
│   ├── ring_buffer.py            # deque sliding window + freq counters + sig set
│   └── parser.py                 # Raw log line → normalized log object
│
├── trigger/
│   ├── __init__.py
│   ├── engine.py                 # Rule evaluation loop
│   ├── rules.py                  # All trigger rule implementations
│   └── cooldown.py               # Per-trigger cooldown tracker
│
├── agent/
│   ├── __init__.py
│   ├── agent.py                  # Async agent — KB search + LLM call
│   ├── context.py                # Context packet assembly
│   ├── kb.py                     # ChromaDB knowledge base read/write
│   ├── prompts.py                # System + user prompt templates
│   └── worker.py                 # Background async worker consuming trigger queue
│
├── output/
│   ├── __init__.py
│   ├── terminal.py               # Rich-formatted terminal alert display
│   ├── webhook.py                # Slack / PagerDuty / custom POST
│   └── store.py                  # JSONL append-only alert log writer
│
├── tests/
│   ├── test_parser.py
│   ├── test_ring_buffer.py
│   ├── test_trigger_rules.py
│   ├── test_cooldown.py
│   ├── test_agent.py             # Uses mocked LLM client
│   ├── test_kb.py
│   └── fixtures/
│       ├── sample_app.log
│       └── sample_alerts.jsonl
│
├── .logwhisper.yaml
├── pyproject.toml
├── README.md
└── LICENSE
```

---

## 14. Build Order

Build and test each module independently before wiring to the next.

```
Step 1 → buffer/parser.py
         Foundation. Everything depends on normalized log objects.
         Test: feed raw log lines, assert correct ts/level/fields/signature output.

Step 2 → buffer/ring_buffer.py
         Depends on: parser.py
         Test: push 600 lines into maxlen=500 buffer, assert correct eviction.
               Assert window() correctly filters by time.

Step 3 → ingest/file_adapter.py
         Depends on: parser.py
         Test: tail a temp file, append lines, assert they appear in async stream.

Step 4 → trigger/rules.py + trigger/cooldown.py
         Depends on: ring_buffer.py
         Test: simulate error spike, assert rate_spike fires at threshold.
               Assert cooldown suppresses re-fire within cooldown window.

Step 5 → trigger/engine.py
         Depends on: rules.py, cooldown.py, ring_buffer.py
         Test: full evaluation loop, assert trigger event lands in agent queue.

Step 6 → agent/kb.py
         Depends on: chromadb, sentence-transformers
         Test: store 3 alerts, search with similar query, assert top result matches.

Step 7 → agent/context.py + agent/prompts.py
         Depends on: ring_buffer.py, kb.py
         Test: assert context packet has all required keys, prompt renders without errors.

Step 8 → agent/agent.py
         Depends on: context.py, prompts.py, kb.py
         Test: mock LLM client returning valid JSON, assert finding is correctly parsed.

Step 9 → output/terminal.py
         Depends on: rich
         Test: render a sample finding, assert no exceptions. Visual inspection.

Step 10 → cli.py
          Depends on: all modules above
          Test: end-to-end integration with sample log file fixture.
```

---

## 15. Module Specifications

### `buffer/parser.py`

**Input:** raw log line string  
**Output:** normalized log object dict  
**Must handle:** JSON logs, plain text, multi-line stack traces (collapse to one object), lines with no parseable timestamp

### `buffer/ring_buffer.py`

**Must provide:**
- `push(log_obj)` — O(1), thread-safe
- `window(seconds=60)` — returns list filtered by timestamp
- `error_rate(source, bucket_seconds)` — returns int
- `is_new_signature(sig)` — returns bool
- `stats(source)` — returns dict with counts

### `trigger/rules.py`

Each rule class must implement:
- `evaluate(log_obj, buffer) -> bool`
- `cooldown: int` property (default cooldown in seconds)
- `name: str` property

### `agent/agent.py`

- **Must be async** — wrap sync SDK calls in `asyncio.to_thread`
- **Timeout:** 30 seconds max per LLM call; return low-confidence finding on timeout
- **Retry:** 2 retries with exponential backoff on rate limit errors

### `output/terminal.py`

- **Must not block** main stream — `rich.Console.print` is thread-safe
- **Live status bar** updates every second: active sources, alert count, suppression count, KB size, uptime

---

## 16. Data Schemas

### Normalized Log Object

```python
{
    "ts": str,           # ISO8601 UTC timestamp
    "source": str,       # source identifier
    "level": str,        # DEBUG | INFO | WARN | ERROR | FATAL
    "raw": str,          # original log line, unmodified
    "fields": {
        "latency_ms": int | None,
        "cpu_pct": float | None,
        "memory_pct": float | None,
        "status": int | None,
    },
    "signature": str,    # 8-char SHA-1 hex
    "adapter": str       # file | docker | k8s | stdin
}
```

### Trigger Event

```python
{
    "rule": str,               # rule class name
    "source": str,
    "fired_at": str,           # ISO8601 UTC
    "log_obj": dict,           # the log line that triggered
    "buffer_window": list,     # last N seconds of buffer
    "stats": {
        "errors_per_10s": int,
        "unique_error_types": int,
        "total_lines": int
    }
}
```

### Agent Finding

```python
{
    "alert_id": str,               # LW-YYYY-NNNN
    "timestamp": str,              # ISO8601 UTC
    "source": str,
    "trigger": str,
    "confidence": str,             # high | medium | low
    "root_signal": str,
    "caused_by": str,              # 1-2 sentence explanation
    "contributing_factors": list,
    "blast_radius": str,
    "evidence": list,              # ["timestamp: log line", ...]
    "action": str,
    "kb_match": dict | None,
    "suppressed_count": int,
    "latency_ms": int
}
```

### Knowledge Base Entry

```python
{
    "id": str,
    "title": str,
    "date": str,               # YYYY-MM-DD
    "source": str,
    "error_type": str,
    "root_cause": str,
    "resolution": str,
    "confidence": str,
    "tags": list
}
```

---

## 17. Agent Prompt Design

### Design Principles

1. **Evidence-grounded** — cite specific log lines, not plausible-sounding explanations
2. **Structured output** — JSON response makes parsing reliable and testable
3. **Bounded scope** — reason only from what is in the window, not general system knowledge
4. **Confidence-honest** — admit uncertainty rather than hallucinate
5. **Action-oriented** — every finding ends with one concrete step

### Anti-Hallucination Rules

```
STRICT RULES:
- Every claim in "caused_by" must be supported by a line in "evidence"
- Never cite a log line that is not in the provided window
- If you cannot determine the root cause, set:
    confidence: "low"
    caused_by: "Insufficient log context to determine root cause"
- Do not use general knowledge about systems to fill gaps
```

### Prompt Iteration Strategy

After testing against 20 real log samples, adjust based on:

| Problem | Fix |
|---------|-----|
| Agent over-cites (too many evidence lines) | Add: "cite at most 3 evidence lines" |
| Agent invents causes | Add: "if cause not visible in window, state so explicitly" |
| Response too verbose | Add: "keep caused_by under 30 words" |
| Action too vague | Add: "action must include a specific command or file path" |

---

## 18. Knowledge Base Design

### Storage Structure

```
.logwhisper/
├── kb/                        # ChromaDB persisted to disk
│   ├── chroma.sqlite3
│   └── ...
└── alerts.jsonl               # All alerts in append-only JSONL
```

### Embedding Strategy

- **Model:** `all-MiniLM-L6-v2` (90MB, CPU-only, 384-dimensional vectors)
- **What is embedded:** `root_signal + " " + caused_by + " " + action`
- **Search query:** raw error message from the current trigger log line

### Similarity Thresholds

| Score | Behaviour |
|-------|-----------|
| > 0.85 | High confidence match — show in alert as "KB match" |
| 0.70 – 0.85 | Weak match — shown as "possibly related to..." |
| < 0.70 | No match shown |

### KB Growth Strategy

- `confidence: high` → stored automatically after every alert
- `confidence: medium` → stored with a human-review flag
- `confidence: low` → NOT stored automatically (too noisy)
- Manual override: `logwhisper kb add` to force-store any alert

---

## 19. Alert Format Specification

### Terminal (Rich)

```
╔══════════════════════════════════════════════════════════╗
║  ⚠  ANOMALY DETECTED   08:14:52 → 08:15:03  [HIGH]      ║
╠══════════════════════════════════════════════════════════╣
║  Trigger       rate_spike · auth-service · 47 err/11s    ║
║  Root signal   TypeError: null session (session.js:142)  ║
║  Caused by     Redis TTL=0 → cache miss → null object    ║
║  Evidence      08:14:47 WARN redis: cache miss u_891     ║
║  Blast radius  checkout, profile, notifications (3)      ║
║  Action        Set REDIS_TTL=3600 in .env and restart    ║
║  KB match      2025-02-11 · similarity: 0.91             ║
║  Suppressed    41 duplicate alerts in cooldown           ║
╚══════════════════════════════════════════════════════════╝
```

### Slack Webhook

```json
{
  "blocks": [
    { "type": "header", "text": { "type": "plain_text", "text": "⚠ Log Whisperer Alert" } },
    {
      "type": "section",
      "fields": [
        { "type": "mrkdwn", "text": "*Source:* auth-service" },
        { "type": "mrkdwn", "text": "*Trigger:* rate_spike (47/10s)" },
        { "type": "mrkdwn", "text": "*Root signal:* TypeError: null session" },
        { "type": "mrkdwn", "text": "*Action:* Set REDIS_TTL=3600 in .env" }
      ]
    }
  ]
}
```

---

## 20. Extension Points

### Adding a Custom Trigger Rule

```python
# trigger/rules.py
class CustomPatternRule:
    name = "custom_pattern"
    cooldown = 120

    def __init__(self, pattern: str):
        self.regex = re.compile(pattern)

    def evaluate(self, log_obj: dict, buffer: RingBuffer) -> bool:
        return bool(self.regex.search(log_obj["raw"]))
```

Register in config:

```yaml
triggers:
  custom:
    pattern: "payment.*declined.*fraud"
    cooldown_seconds: 60
```

### Adding a Custom Output Destination

```python
# output/custom.py
class CustomOutput:
    async def send(self, finding: dict):
        # POST to your internal system, write to DB, etc.
        ...
```

### Adding a New Source Adapter

```python
class MySourceAdapter(BaseAdapter):
    async def stream(self) -> AsyncIterator[dict]:
        async for raw_line in my_source.lines():
            log_obj = parser.parse(raw_line, source="my-source")
            yield log_obj
```

---

## 21. Evaluation Benchmarks

### Test Scenarios

| # | Scenario | Expected Result |
|---|---------|----------------|
| 1 | Rate spike: 50 errors in 5 seconds | `rate_spike` triggers, root cause identified |
| 2 | New error type appears | `new_error_type` triggers within 1 second |
| 3 | `FATAL: OOM` in one line | `critical_keyword` triggers immediately |
| 4 | `latency=3500ms` in log line | `metric_threshold` triggers (limit: 2000ms) |
| 5 | 4 services emit errors in 20 seconds | `cluster_formation` triggers, all 4 listed in blast_radius |
| 6 | Active source silent for 90 seconds | `silence_anomaly` triggers for that source |
| 7 | Same error fires 200 times in 2 minutes | Exactly 1 alert, suppressed count = 199 |
| 8 | Error matches KB entry from 30 days ago | KB match shown with similarity score |
| 9 | Docker + file watched simultaneously | Both sources correctly attributed |
| 10 | 10,000 lines/minute throughput | No memory growth, trigger < 1ms, agent < 5s |

### Performance Targets

| Metric | Target |
|--------|--------|
| Lines processed per second | 10,000+ |
| Trigger evaluation latency | < 1ms per line |
| Agent call latency (p50) | < 3 seconds |
| Agent call latency (p95) | < 8 seconds |
| Memory usage (500-line buffer) | < 50MB |
| False positive rate | < 10% of alerts |
| KB search latency | < 200ms |

---

## 22. Bonus Features

| Feature | Description | Complexity |
|---------|-------------|-----------|
| **Predictive mode** | Analyze WARN-level trends and predict an ERROR spike before it happens | High |
| **Auto-runbook generator** | After 3 alerts of the same class, draft a step-by-step runbook for that failure | Medium |
| **VS Code extension** | Wrap the CLI in a VS Code extension — alerts appear as inline editor notifications | High |
| **Multi-session learning** | KB improves across sessions — agent notices if the same fix keeps recurring | Medium |
| **Slack bot mode** | Long-running bot — developers ask questions about the current session via Slack | Medium |
| **Post-mortem export** | `logwhisper pm --incident LW-2025-089` generates a full post-mortem draft | Medium |
| **Diff mode** | Compare two log files from different deployments to isolate regressions | Low |
| **GNS3/network sim integration** | Pull live syslogs from a network simulator for infrastructure log analysis | High |

---

## Quick Start

```bash
# 1. Install
pip install -e .

# 2. Set API key
export ANTHROPIC_API_KEY=sk-ant-...

# 3. Initialize project config
logwhisper config init

# 4. Watch a file
logwhisper watch --file ./logs/app.log

# 5. Watch a Docker container
logwhisper watch --docker my-service

# 6. Watch all pods in a k8s namespace
logwhisper watch --k8s prod --all-pods

# 7. Search past incidents
logwhisper kb search "redis connection refused"

# 8. Analyze a historical log file
logwhisper analyze --file ./logs/incident.log --since "08:00" --until "09:30"
```

---

*Log Whisperer — an AI agent that watches your logs so you don't have to.*
