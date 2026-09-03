"""Canal de terminal: um REPL para conversar com o agente sem WhatsApp.

É o canal de demonstração — `emit` imprime, `Inbound` vem do stdin. A flag
`--script` roda um roteiro (uma mensagem por linha) sem interação, que é como se
gera o log JSONL de execução da entrega; `--delay` espaça as mensagens do roteiro
para caber na cota gratuita do Gemini (5 req/min, e cada turno gasta 2 chamadas).
"""
from __future__ import annotations

import argparse
import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any

from agent.brain import Extractor, Responder, resumo_state
from agent.config import settings
from agent.conversation import Conversation, InMemoryStateStore
from agent.models import Inbound, LeadState, Outbound
from agent.pii import mask_text
from agent.quote_client import QuoteClient, QuotePlanosUnavailable
from agent.rules import Rules
from agent.runtime_config import store

PROMPT = "você: "
AJUDA = "Comandos: /estado (dados coletados), /audio (simula um áudio), /sair."


class BootError(RuntimeError):
    """Falha de inicialização com mensagem já pronta para o operador."""


def _params_quote_client() -> dict:
    """Parâmetros do cliente relidos do store a cada chamada (o Studio muda em tempo real)."""
    return {
        "timeout_s": store.param("tools.quote_client.timeout_s"),
        "max_attempts": store.param("tools.quote_client.max_attempts"),
        "budget_s": store.param("tools.quote_client.budget_s"),
        "backoff_base_s": store.param("tools.quote_client.backoff_base_s"),
        "planos_ttl_s": store.param("tools.quote_client.planos_ttl_s"),
    }


def _provedor_de_regras(client: QuoteClient, today: Callable[[], date]) -> Callable[[], Awaitable[Rules]]:
    """Fecha sobre o client e devolve as `Rules` de AGORA: catálogo com TTL + `today()` novo.

    Duas coisas estragam regra derivada: um `/planos` que mudou e um processo que atravessou a
    virada do ano com o `today` do boot congelado (em 1º de janeiro um carro que era aceito
    deixa de ser). O memo é por (catálogo, dia): dentro do mesmo dia e do mesmo catálogo, o
    `Rules` devolvido é literalmente o mesmo objeto, então reler é de graça.
    """
    memo: dict[str, Any] = {}

    async def provider() -> Rules:
        snapshot = await client.planos()
        chave = (id(snapshot.planos), today())
        if memo.get("chave") != chave:
            memo["chave"] = chave
            memo["rules"] = Rules.from_planos(snapshot.planos, today())
        return memo["rules"]

    return provider


async def montar_conversa(
    today: Callable[[], date] = date.today,
    *,
    base_url: str | None = None,
    trace: Callable[[dict], None] | None = None,
    logger_factory: Callable[..., object] | None = None,
    on_handoff: Callable[..., object] | None = None,
) -> Conversation:
    """Carrega settings, exige a chave do LLM e deriva as regras do `/planos` da API.

    `base_url` (o Lab aponta cada sessão para uma API), `trace` (hook de observação das
    chamadas ao LLM), `logger_factory` e `on_handoff` (aviso ao consultor) são pontos de
    injeção; sem eles o boot é exatamente o do canal de terminal — no CLI ninguém é avisado.
    """
    if not settings.google_api_key:
        raise BootError("GOOGLE_API_KEY não configurada. Copie .env.example para .env e preencha a chave.")

    api = base_url or store.param("tools.quote_client.base_url")
    p = _params_quote_client()
    client = QuoteClient(
        api,
        timeout_s=p["timeout_s"],
        max_attempts=p["max_attempts"],
        budget_s=p["budget_s"],
        backoff_base_s=p["backoff_base_s"],
        planos_ttl_s=p["planos_ttl_s"],
        params=_params_quote_client,
    )
    rules_provider = _provedor_de_regras(client, today)
    try:
        planos = await client.get_planos()
    except QuotePlanosUnavailable as exc:
        raise BootError(
            f"Não consegui ler {api}/planos ({exc}). "
            "Suba a API de cotação antes de abrir o chat — as regras de aceitação saem dela."
        ) from exc

    return Conversation(
        rules=Rules.from_planos(planos, today()),
        quote_client=client,
        extractor=Extractor(trace=trace),
        responder=Responder(trace=trace, planos=planos),   # coberturas reais nos guardrails
        log_dir=settings.log_dir,
        store=InMemoryStateStore(),
        today=today,
        logger_factory=logger_factory,
        on_handoff=on_handoff,
        rules_provider=rules_provider,
    )


def _resumo(state: LeadState | None) -> str:
    return mask_text(resumo_state(state)) if state else "(nada coletado ainda)"


async def conversar(
    conv: Conversation,
    conversation_id: str,
    mensagens: list[str] | None = None,
    delay: float = 0.0,
) -> None:
    """Loop de turnos. Com `mensagens`, roda o roteiro; sem elas, lê o stdin.

    `delay` só vale no roteiro: é o intervalo entre mensagens, para não estourar a
    cota por minuto do provedor de LLM.
    """

    ultima_saida = ""

    async def emit(out: Outbound) -> None:
        nonlocal ultima_saida
        ultima_saida = out.text
        print(f"🤖 {out.text}\n")

    roteiro = list(mensagens) if mensagens is not None else None
    repeticoes = 0  # no roteiro, reenvia a linha quando o agente pede para repetir (LLM instável)
    turno = 0
    primeira = True
    while True:
        if roteiro is not None:
            if not roteiro:
                break
            if delay > 0 and not primeira:
                print(f"[aguardando {delay:g}s]")
                await asyncio.sleep(delay)
            primeira = False
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
            inbound = Inbound(
                conversation_id=conversation_id, message_id=f"m{turno}", media_type="audio", origem="cli"
            )
        elif not linha:
            turno -= 1
            continue
        else:
            inbound = Inbound(conversation_id=conversation_id, message_id=f"m{turno}", text=linha, origem="cli")

        await conv.handle(inbound, emit)
        if roteiro is not None and ultima_saida == store.text("policy.txt_instabilidade") and repeticoes < 2:
            repeticoes += 1
            print(f"[roteiro: agente pediu para repetir, reenviando ({repeticoes}/2)]")
            roteiro.insert(0, linha)


async def run(script: Path | None = None, conversation_id: str | None = None, delay: float | None = None) -> int:
    try:
        conv = await montar_conversa()
    except BootError as exc:
        print(f"[erro] {exc}")
        return 2

    if delay is None:
        delay = float(store.param("settings.script_delay_s"))
    cid = conversation_id or f"cli-{int(time.time())}"
    mensagens: list[str] | None = None
    if script is not None:
        linhas = script.read_text(encoding="utf-8").splitlines()
        mensagens = [linha.strip() for linha in linhas if linha.strip() and not linha.startswith("#")]

    print(f"AutoSeguro — conversa {cid}. {AJUDA}\n")
    await conversar(conv, cid, mensagens, delay=delay)
    print(f"[log] {settings.log_dir / f'{cid}.jsonl'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.chat", description="Chat de terminal do agente AutoSeguro")
    parser.add_argument("--script", type=Path, help="arquivo com uma mensagem por linha (roda sem interação)")
    parser.add_argument("--conversation-id", help="id fixo da conversa (padrão: cli-<timestamp>)")
    parser.add_argument(
        "--delay",
        type=float,
        default=None,
        help="segundos de espera entre as mensagens do --script (cota do LLM); padrão: settings.script_delay_s",
    )
    args = parser.parse_args(argv)
    return asyncio.run(run(script=args.script, conversation_id=args.conversation_id, delay=args.delay))
