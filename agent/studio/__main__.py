"""`uv run python -m agent.studio` — sobe o Studio. Só em 127.0.0.1: é painel de operador,
não canal do agente (isso é `agent/serve.py`, que nunca importa este pacote).
"""
from __future__ import annotations

import os

import uvicorn

from agent.studio.app import build_studio_app

app = build_studio_app()


def main() -> None:
    porta = int(os.getenv("STUDIO_PORT", "8765"))
    uvicorn.run(app, host="127.0.0.1", port=porta)


if __name__ == "__main__":
    main()
