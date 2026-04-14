# Log Whisperer

**AI-Powered Live Log Intelligence Agent** — watches your log streams in real-time and fires structured, evidence-backed anomaly alerts using an LLM.

Unlike traditional log monitoring tools that match static rules, Log Whisperer reasons about what is actually happening: root cause, blast radius, and what to do right now.

---

## Features

- **Live log tailing** — file, Docker containers, Kubernetes pods, or stdin pipe
- **AI-powered analysis** — MiniMax / Claude / GPT-4o / Ollama
- **Windows toast notifications** — get alerted even when the terminal is minimized
- **Rich terminal UI** — color-coded alerts with evidence, action, and blast radius
- **Smart triggering** — only wakes the LLM when something is actually wrong (not every log line)
- **Cooldown suppression** — 200 identical errors = 1 alert with suppressed count
- **Local-first** — no cloud infra required, just an LLM API call

---

## Requirements

- Python 3.11+
- Windows (for toast notifications via `winotify`)
- MiniMax API key (or OpenAI/Anthropic/Ollama)

---

## Installation

### 1. Clone / navigate to the project

```bash
cd "D:\Personal Projects\Log Whisperer"
```

### 2. Create a virtual environment

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # macOS / Linux
```

### 3. Install dependencies

```bash
pip install rich typer PyYAML python-dateutil winotify httpx
```

### 4. Set up Log Whisperer

**One-time guided setup (recommended):**
```bash
logwhisper setup
```

This interactive command will:
- Ask for your MiniMax API key (with a link to get one)
- Ask if you want to enable Telegram alerts (with step-by-step instructions)
- Validate everything and print a summary

**Or set the API key directly:**
```bash
# Windows
set MINIMAX_API_KEY=your_minimax_api_key_here

# macOS / Linux
export MINIMAX_API_KEY=your_minimax_api_key_here
```

**Pass it to a command:**
```bash
logwhisper watch --file ./test.log --api-key your_key_here
```

---

## Quick Start

### Watch a log file

```bash
logwhisper watch --file ./test.log
```

### Watch multiple files

```bash
python cli.py watch --file ./logs/app.log --file ./logs/auth.log
```

### Watch with custom trigger threshold

```bash
python cli.py watch --file ./logs/app.log --buffer 1000
```

### Run without Windows notifications

```bash
python cli.py watch --file ./logs/app.log --no-notification
```

---

## CLI Reference

### `setup` — First-time setup

```bash
logwhisper setup
```

| Option | Description |
|--------|-------------|
| `--skip-telegram` | Skip Telegram configuration |
| `--skip-api-key` | Skip API key configuration |

### `chat` — Interactive error analysis & project Q&A

```bash
logwhisper chat
```

```bash
logwhisper chat --error "KeyError: 'user' in /predict"
```

```bash
logwhisper chat --file ./test.log
```

| Option | Description |
|--------|-------------|
| `--error`, `-e` | Single-shot: analyze a specific error or log excerpt |
| `--file`, `-f` | Load recent log lines from a file for context |

In interactive mode:
- Type `exit` / `quit` to leave
- Type `context` to see project files being used

### `watch` — Live monitoring

```bash
logwhisper watch [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--file`, `-f` | — | Log file(s) to tail |
| `--poll` | 100 | File polling interval (ms) |
| `--buffer`, `-b` | 500 | Ring buffer max lines |
| `--model` | MiniMax-Text-01 | LLM model name |
| `--api-key` | env/MINIMAX_API_KEY | API key |
| `--no-notification` | false | Disable Windows toast |
| `--no-telegram` | false | Disable Telegram alerts |

### `analyze` — Batch analysis

```bash
logwhisper analyze --file ./logs/incident.log
```

> Batch analysis is planned — currently use `watch` for live analysis.

---

## Configuration

Create a `.logwhisper.yaml` in your project root:

```yaml
llm:
  provider: minimax          # minimax | anthropic | openai | ollama
  model: MiniMax-Text-01
  api_key: ${MINIMAX_API_KEY}
  max_tokens: 1000
  timeout_seconds: 30

buffer:
  max_lines: 500
  context_window_seconds: 60

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

output:
  terminal: true
  windows_notification: true
```

---

## Project Structure

```
logwhisper/
├── cli.py                     # Typer CLI — entry point
├── config.py                  # .logwhisper.yaml loader
│
├── buffer/
│   ├── parser.py              # Raw log → normalized log object
│   └── ring_buffer.py         # Sliding window + frequency tracking
│
├── ingest/
│   └── file_adapter.py        # Async file tail with rotation detection
│
├── trigger/
│   ├── engine.py              # Rule evaluation loop
│   ├── rules.py               # 6 trigger rule types
│   └── cooldown.py            # Per-(rule, source) cooldown tracker
│
├── agent/
│   ├── agent.py               # MiniMax LLM integration
│   └── worker.py              # Background async agent worker
│
└── output/
    ├── terminal.py            # Rich-formatted alert display
    └── windows_notification.py # Windows toast notifications
```

---

## How It Works

```
Log File → [File Adapter] → [Ring Buffer] → [Trigger Engine] → [Agent Queue]
                ↓                  ↓                ↓               ↓
           parser adds      sliding window    fires only when    async LLM
           timestamp/level   500 lines max     something wrong     call
                                                                ↓
                                                    [Windows Toast + Terminal Alert]
```

### Trigger Rules

| Rule | Fires When |
|------|-----------|
| `NewErrorTypeRule` | First appearance of an error signature this session |
| `RateSpikeRule` | Error rate exceeds threshold (default: 5/10s) |
| `CriticalKeywordRule` | Fatal/OOM/panic/deadlock keywords detected |
| `MetricThresholdRule` | latency_ms / cpu_pct / memory_pct exceeds limit |
| `ClusterFormationRule` | Errors from 3+ sources in 30s window |
| `SilenceAnomalyRule` | Active source stops emitting for 60s+ |

---

## Alert Output

When an alert fires, you see:

```
╔══════════════════════════════════════════════════════════╗
║  🔴 ANOMALY DETECTED  2026-04-08T10:00:10              ║
╠══════════════════════════════════════════════════════════╣
║  Trigger       CriticalKeywordRule — test.log           ║
║  Root signal   FATAL Out of memory                       ║
║  Caused by     System ran out of available memory       ║
║  Evidence      2026-04-08 FATAL Out of memory           ║
║  Action        Check for memory leaks or increase RAM    ║
╚══════════════════════════════════════════════════════════╝
```

Plus a Windows toast notification with the same info.

---

## Supported LLM Providers

```bash
# MiniMax (default)
logwhisper watch --file ./logs/app.log --model MiniMax-Text-01

# OpenAI GPT-4o
logwhisper watch --file ./logs/app.log --model gpt-4o

# Anthropic Claude
logwhisper watch --file ./logs/app.log --model claude-sonnet-4-20250514

# Ollama (local)
logwhisper watch --file ./logs/app.log --model llama3
```

---

## Troubleshooting

### "No module named 'X'"
```bash
pip install rich typer PyYAML python-dateutil winotify httpx
```

### "No API key provided"
```bash
set MINIMAX_API_KEY=your_key_here
logwhisper watch --file ./logs/app.log
```

### Toast notifications not showing
- Make sure `winotify` is installed: `pip install winotify`
- Check Windows notification settings for your account

### File not being tailed
- Ensure the file path exists and is readable
- Try `--poll 500` if the file is written slowly

---

## License

MIT
