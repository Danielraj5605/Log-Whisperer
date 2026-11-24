"""Allow `python -m agent ...` (same as the `logwhisper` console script)."""

from agent import app

if __name__ == "__main__":
    app(prog_name="logwhisper")
