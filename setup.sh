#!/usr/bin/env bash
# ============================================================
#  Log Whisperer — macOS / Linux Setup Script
#  Run this once after cloning the repo:  ./setup.sh
# ============================================================

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
RESET='\033[0m'

echo ""
echo -e "${BOLD} =====================================================${RESET}"
echo -e "${BOLD}  Log Whisperer | First-Time Setup${RESET}"
echo -e "${BOLD} =====================================================${RESET}"
echo ""

# ── Step 1: Check Python ──────────────────────────────────────
echo -e "${CYAN}[1/5]${RESET} Checking Python version..."

# Try python3 first, then python
PYTHON_BIN=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON_BIN="$candidate"
        break
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo ""
    echo -e "${RED}[ERROR]${RESET} Python not found!"
    echo ""
    echo "  Please install Python 3.11 or newer:"
    echo "  macOS:  brew install python@3.11"
    echo "  Ubuntu: sudo apt install python3.11"
    echo "  Or download from: https://www.python.org/downloads/"
    echo ""
    exit 1
fi

PYVER=$($PYTHON_BIN --version 2>&1 | awk '{print $2}')
PYMAJOR=$(echo "$PYVER" | cut -d. -f1)
PYMINOR=$(echo "$PYVER" | cut -d. -f2)

if [ "$PYMAJOR" -lt 3 ] || { [ "$PYMAJOR" -eq 3 ] && [ "$PYMINOR" -lt 11 ]; }; then
    echo ""
    echo -e "${RED}[ERROR]${RESET} Python $PYVER is too old. Log Whisperer needs Python 3.11+"
    echo "  Download: https://www.python.org/downloads/"
    echo ""
    exit 1
fi

echo -e "${GREEN}[OK]${RESET} Python $PYVER found."
echo ""

# ── Step 2: Create virtual environment ───────────────────────
echo -e "${CYAN}[2/5]${RESET} Creating virtual environment (.venv)..."

if [ -d ".venv" ]; then
    echo -e "${GREEN}[OK]${RESET} .venv already exists, skipping creation."
else
    $PYTHON_BIN -m venv .venv
    echo -e "${GREEN}[OK]${RESET} Virtual environment created."
fi
echo ""

# ── Step 3: Activate and upgrade pip ─────────────────────────
echo -e "${CYAN}[3/5]${RESET} Activating virtual environment and upgrading pip..."

source .venv/bin/activate
python -m pip install --upgrade pip --quiet
echo -e "${GREEN}[OK]${RESET} pip is up to date."
echo ""

# ── Step 4: Install Log Whisperer and all dependencies ───────
echo -e "${CYAN}[4/5]${RESET} Installing Log Whisperer and dependencies..."
echo "  (This may take a minute on first run)"
echo ""

pip install -e . --quiet
echo -e "${GREEN}[OK]${RESET} All dependencies installed."
echo ""

# ── Step 5: Run guided setup ──────────────────────────────────
echo -e "${CYAN}[5/5]${RESET} Running first-time configuration..."
echo ""
echo "  You will be asked for your Gemini API key."
echo "  Get one for free at: https://aistudio.google.com/app/apikey"
echo ""

logwhisper setup
echo ""

# ── Done ──────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN} =====================================================${RESET}"
echo -e "${BOLD}${GREEN}  Setup complete!${RESET}"
echo -e "${BOLD}${GREEN} =====================================================${RESET}"
echo ""
echo -e " Activate the environment (do this each new terminal):"
echo -e "   ${CYAN}source .venv/bin/activate${RESET}"
echo ""
echo -e " Watch a log file for anomalies:"
echo -e "   ${CYAN}logwhisper watch --file ./test.log${RESET}"
echo ""
echo -e " Interactive AI chat about errors:"
echo -e "   ${CYAN}logwhisper chat${RESET}"
echo ""
echo -e " Full monitoring mode (auto-detect project):"
echo -e "   ${CYAN}logwhisper run${RESET}"
echo ""
echo -e "${BOLD} =====================================================${RESET}"
echo ""
