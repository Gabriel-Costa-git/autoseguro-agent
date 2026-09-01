"""Canal de terminal: um REPL para conversar com o agente sem WhatsApp.

É o canal de demonstração — `emit` imprime, `Inbound` vem do stdin. A flag
`--script` roda um roteiro (uma mensagem por linha) sem interação, que é como se
gera o log JSONL de execução da entrega.
"""
from __future__ import annotations

import argparse
import asyncio
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path

from agent.brain import Extractor, Responder, resumo_state
from agent.config import settings
from agent.conversation import Conversation, InMemoryStateStore
from agent.models import Inbound, LeadState, Outbound
from agent.pii import mask_text
from agent.quote_client import QuoteClient, QuotePlanosUnavailable
from agent.rules import Rules

PROMPT = "você: "
AJUDA = "Comandos: /estado (dados coletados), /audio (simula um áudio), /sair."


class BootError(RuntimeError):
    """Falha de inicialização com mensagem já pronta para o operador."""


async def montar_conversa(today: Callable[[], date] = date.today) -> Conversation:
    """Carrega settings, exige a chave do LLM e deriva as regras do `/planos` da API."""
    if not settings.google_api_key:
        raise BootError("GOOGLE_API_KEY não configurada. Copie .env.example para .env e preencha a chave.")

    client = QuoteClient(
        settings.quote_api_url,
        timeout_s=settings.quote_timeout_s,
        max_attempts=settings.quote_max_attempts,
        budget_s=settings.quote_budget_s,
        backoff_base_s=settings.quote_backoff_base_s,
    )
    try:
        planos = await client.get_planos()
    except QuotePlanosUnavailable as exc:
        raise BootError(
            f"Não consegui ler {settings.quote_api_url}/planos ({exc}). "
            "Suba a API de cotação antes de abrir o chat — as regras de aceitação saem dela."
        ) from exc

    return Conversation(
        rules=Rules.from_planos(planos, today()),
        quote_client=client,
        extractor=Extractor(),
        responder=Responder(),
        log_dir=settings.log_dir,
        store=InMemoryStateStore(),
        today=today,
    )


def _resumo(state: LeadState | None) -> str:
    return mask_text(resumo_state(state)) if state else "(nada coletado ainda)"


async def conversar(conv: Conversation, conversation_id: str, mensagens: list[str] | None = None) -> None:
    """Loop de turnos. Com `mensagens`, roda o roteiro; sem elas, lê o stdin."""

    async def emit(out: Outbound) -> None:
        print(f"🤖 {out.text}\n")

    roteiro = list(mensagens) if mensagens is not None else None
    turno = 0
    while True:
        if roteiro is not None:
            if not roteiro:
                break
            linha = roteiro.pop(0)
            print(f"{PROMPT}{linha}")
        else:
            try:
                linha = (await asyncio.to_thread(input, PROMPT)).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

        comando = linha.lower()
        if comando in ("/sair", "/quit", "/exit"):
            break
        if comando == "/ajuda":
            print(AJUDA)
            continue
        if comando == "/estado":
            print(f"[estado] {_resumo(conv.store.get(conversation_id))}\n")
            continue

        turno += 1
        if comando == "/audio":
            inbound = Inbound(conversation_id=conversation_id, message_id=f"m{turno}", media_type="audio")
        elif not linha:
            turno -= 1
            continue
        else:
            inbound = Inbound(conversation_id=conversation_id, message_id=f"m{turno}", text=linha)

        await conv.handle(inbound, emit)


async def run(script: Path | None = None, conversation_id: str | None = None) -> int:
    try:
        conv = await montar_conversa()
    except BootError as exc:
        print(f"[erro] {exc}")
        return 2

    cid = conversation_id or f"cli-{int(time.time())}"
    mensagens: list[str] | None = None
    if script is not None:
        linhas = script.read_text(encoding="utf-8").splitlines()
        mensagens = [linha.strip() for linha in linhas if linha.strip() and not linha.startswith("#")]

    print(f"AutoSeguro — conversa {cid}. {AJUDA}\n")
    await conversar(conv, cid, mensagens)
    print(f"[log] {settings.log_dir / f'{cid}.jsonl'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.chat", description="Chat de terminal do agente AutoSeguro")
    parser.add_argument("--script", type=Path, help="arquivo com uma mensagem por linha (roda sem interação)")
    parser.add_argument("--conversation-id", help="id fixo da conversa (padrão: cli-<timestamp>)")
    args = parser.parse_args(argv)
    return asyncio.run(run(script=args.script, conversation_id=args.conversation_id))
