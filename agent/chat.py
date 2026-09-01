"""Entrypoint do chat de terminal: `uv run python -m agent.chat [--script roteiro.txt]`."""
from __future__ import annotations

from agent.channels.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
