"""Entrypoint HTTP: `uv run python -m agent.serve` sobe o webhook do WhatsApp (Evolution API).

Reusa `agent.channels.cli.montar_conversa` — a mesma função que monta o `Conversation`
para o chat de terminal — para não duplicar a lógica de boot (LLM, regras derivadas
do `/planos`, logger). Só troca o canal: aqui é o webhook Evolution, lá era o stdin.
"""
from __future__ import annotations

import asyncio
import os

import uvicorn

from agent.channels.cli import BootError, montar_conversa
from agent.channels.evolution import EvolutionSender, build_app
from agent.config import settings
from agent.runtime_config import CONFIG_DIR
from agent.takeover import TakeoverStore


def _exigir_settings() -> None:
    """Aborta com mensagem clara se faltar algo — nada de subir meio-configurado."""
    faltando = [
        nome
        for nome, valor in (
            ("EVOLUTION_URL", settings.evolution_url),
            ("EVOLUTION_APIKEY", settings.evolution_apikey),
            ("EVOLUTION_INSTANCE", settings.evolution_instance),
            ("GOOGLE_API_KEY", settings.google_api_key),
        )
        if not valor
    ]
    if faltando:
        raise BootError(
            f"variáveis obrigatórias ausentes no ambiente: {', '.join(faltando)}. "
            "Copie .env.example para .env e preencha."
        )


def run() -> int:
    try:
        _exigir_settings()
        conversation = asyncio.run(montar_conversa())
    except BootError as exc:
        print(f"[erro] {exc}")
        return 2

    sender = EvolutionSender(
        base_url=settings.evolution_url,  # type: ignore[arg-type]  # já validado em _exigir_settings
        apikey=settings.evolution_apikey,  # type: ignore[arg-type]
        instance=settings.evolution_instance,  # type: ignore[arg-type]
    )
    # Conversa assumida por um humano no painel do operador não passa pelo agente.
    # Sem `config/atendimentos.json` (o caso normal) o mapa é vazio e nada muda.
    app = build_app(conversation, sender, takeover=TakeoverStore(CONFIG_DIR))

    porta = int(os.getenv("PORT", "3000"))
    uvicorn.run(app, host="0.0.0.0", port=porta)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
