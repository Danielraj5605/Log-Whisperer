# Log Whisperer

**AI-Powered Live Log Intelligence Agent** — watches your log streams in real-time and fires structured, evidence-backed anomaly alerts using an LLM.

Unlike traditional log monitoring tools that match static rules, Log Whisperer *reasons* about what is actually happening: root cause, blast radius, and what to do right now.

---

## Features

- **Live log tailing** — file, Docker containers, or stdin pipe
- **AI-powered analysis** — Gemini / Claude / GPT-4o / Ollama
- **Windows toast notifications** — get alerted even when the terminal is minimized
- **Rich terminal UI** — color-coded alerts with evidence, action, and blast radius
- **Smart triggering** — only wakes the LLM when something is actually wrong (not every log line)
- **Cooldown suppression** — 200 identical errors = 1 alert with a suppressed count
- **Local-first** — no cloud infra required, just a Gemini API key

---

## Requirements

- Python 3.11+
- A free [Gemini API key](https://aistudio.google.com/app/apikey)
- Windows, macOS, or Linux

> **Note:** Windows toast notifications require the `winotify` package (installed automatically). On macOS/Linux, notifications are terminal-only.

---

## Quick Setup (Recommended)

### Windows

```bat
git clone https://github.com/your-username/log-whisperer.git
cd log-whisperer
setup.bat
```

`setup.bat` will automatically:
1. Verify Python 3.11+ is installed
2. Create a virtual environment (`.venv`)
3. Install all dependencies
4. Run the interactive `logwhisper setup` wizard

### macOS / Linux

```bash
git clone https://github.com/your-username/log-whisperer.git
cd log-whisperer
chmod +x setup.sh
./setup.sh
```

Same steps as above — just double-run `source .venv/bin/activate` in each new terminal session before using `logwhisper`.

---

## Manual Setup (Advanced)

If you prefer to set things up yourself:

```bash
# 1. Create and activate a virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 2. Install Log Whisperer and all dependencies
pip install -e .

# 3. Run the guided first-time setup
logwhisper setup
```

### Setting your API key without the wizard

```bash
# Windows
set GEMINI_API_KEY=your_gemini_api_key_here

# macOS / Linux
export GEMINI_API_KEY=your_gemini_api_key_here
```

Or pass it directly to a command:
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
logwhisper watch --file ./logs/app.log --file ./logs/auth.log
```

### Watch with a custom trigger threshold

```bash
logwhisper watch --file ./logs/app.log --buffer 1000
```

### Run without Windows notifications

```bash
logwhisper watch --file ./logs/app.log --no-notification
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
| `--model` | gemini-2.5-flash | LLM model name |
| `--api-key` | env/GEMINI_API_KEY | Gemini API key |
| `--no-notification` | false | Disable Windows toast |
| `--no-telegram` | false | Disable Telegram alerts |

### `analyze` — Batch analysis

```bash
logwhisper analyze --file ./logs/incident.log
```

> Batch analysis is planned — currently use `watch` for live analysis.

### `telegram` — Manage Telegram alerts

```bash
logwhisper telegram status   # Show current config
logwhisper telegram test     # Send a test message
logwhisper telegram clear    # Remove Telegram config
```

---

## Configuration

Create a `.logwhisper.yaml` in your project root:

```yaml
llm:
  provider: gemini
  model: gemini-2.5-flash
  api_key: ${GEMINI_API_KEY}
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
log-whisperer/
├── setup.bat                  # One-click Windows setup
├── setup.sh                   # One-click macOS/Linux setup
├── .env.example               # Environment variable template
├── requirements.txt           # Pip fallback dependency list
├── pyproject.toml             # Package metadata & entry points
├── config.py                  # .logwhisper.yaml loader
│
├── agent/
│   ├── __init__.py            # Typer CLI — entry point
│   ├── agent.py               # Gemini LLM integration
│   ├── chat.py                # Interactive chat agent
│   ├── deps.py                # Dependency checker
│   ├── detect.py              # Project type detection
│   ├── repl.py                # Interactive REPL session
│   ├── run.py                 # Auto-detect & run session
│   ├── session.py             # API key & config management
│   ├── shell.py               # Shell command runner
│   └── worker.py              # Background agent worker
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
└── output/
    ├── terminal.py            # Rich-formatted alert display
    ├── dashboard.py           # Live status dashboard
    ├── telegram_notification.py # Telegram alerts
    └── windows_notification.py  # Windows toast notifications
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
# Gemini (default)
logwhisper watch --file ./logs/app.log --model gemini-2.5-flash

# OpenAI GPT-4o
logwhisper watch --file ./logs/app.log --model gpt-4o

# Anthropic Claude
logwhisper watch --file ./logs/app.log --model claude-sonnet-4-20250514

# Ollama (local)
logwhisper watch --file ./logs/app.log --model llama3
```

---

## Troubleshooting

### ❌ `'logwhisper' is not recognized as a command`

The CLI entry point is only available after running `pip install -e .` inside your virtual environment. Just re-run `setup.bat` (or `setup.sh`) — it handles this automatically.

Alternatively:
```bash
.venv\Scripts\activate     # Windows
pip install -e .
```

### ❌ `No module named 'X'`

Your virtual environment may not be activated. Activate it first:
```bash
# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate
```
Then retry your command.

### ❌ `No API key provided`

Either run the setup wizard:
```bash
logwhisper setup
```

Or set the environment variable:
```bash
# Windows
set GEMINI_API_KEY=your_key_here

# macOS / Linux
export GEMINI_API_KEY=your_key_here
```

Get a free Gemini API key at: https://aistudio.google.com/app/apikey

### ❌ Python version error

Log Whisperer requires **Python 3.11 or newer**. Check your version:
```bash
python --version
```

Download the latest Python from: https://www.python.org/downloads/

### ❌ Toast notifications not showing

- Make sure `winotify` is installed: `pip install winotify`
- Check Windows notification settings → ensure notifications are enabled for your terminal app

### ❌ File not being tailed

- Ensure the file path exists and is readable
- Try `--poll 500` if the file is written slowly
- Use an absolute path if a relative path isn't being resolved correctly

---

## License

MIT
