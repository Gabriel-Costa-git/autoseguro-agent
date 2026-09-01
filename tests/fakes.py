"""Dublês de teste do turno conversacional.

Enquanto `policy`, `presenter`, `quote_client`, `cep` e `observability` estão sendo
escritos em paralelo, o `conversation.py` é exercitado com estes fakes — que
implementam exatamente as assinaturas de `agent/models.py`. Nada aqui usa rede,
LLM ou docker.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any

from agent.models import (
    Action,
    CepInfo,
    Extraction,
    LeadState,
    PlanoResumo,
    Quote,
    QuoteAttempt,
    QuoteOutcome,
    QuoteRequest,
    QuoteResult,
)

Passo = Callable[[LeadState, Extraction | None], tuple[LeadState, list[Action]]]


# --------------------------------------------------------------------------- LLM
class FakeExtractor:
    """Devolve `Extraction`s de um roteiro (uma por chamada) ou levanta, para testar falha."""

    def __init__(self, roteiro: list[Extraction] | None = None, erro: Exception | None = None) -> None:
        self.roteiro = list(roteiro or [])
        self.erro = erro
        self.chamadas: list[tuple[str, date]] = []

    async def extract(self, text: str, state: LeadState, today: date) -> Extraction:
        self.chamadas.append((text, today))
        if self.erro is not None:
            raise self.erro
        if self.roteiro:
            return self.roteiro.pop(0)
        return Extraction()


class FakeResponder:
    """Ecoa a diretiva recebida — deixa visível no outbound o que a policy pediu."""

    def __init__(self, resposta: str | None = None) -> None:
        self.resposta = resposta
        self.chamadas: list[tuple[str, str]] = []

    async def reply(self, directive: str, state: LeadState, inbound_text: str) -> str:
        self.chamadas.append((directive, inbound_text))
        return self.resposta if self.resposta is not None else f"[llm] {directive}"


# --------------------------------------------------------------------------- policy / presenter
class ScriptedPolicy:
    """`next_action` falso: consome um roteiro de passos e registra o que recebeu."""

    def __init__(self, passos: list[Passo]) -> None:
        self.passos = list(passos)
        self.chamadas: list[Extraction | None] = []

    def __call__(
        self, state: LeadState, extraction: Extraction | None, rules: Any, today: date
    ) -> tuple[LeadState, list[Action]]:
        self.chamadas.append(extraction)
        passo = self.passos.pop(0) if self.passos else (lambda s, e: (s, []))
        return passo(state.model_copy(deep=True), extraction)


def render_fake(action: Action, state: LeadState) -> str:
    """Presenter mínimo: texto determinístico por tipo de ação (preço só do `Quote`)."""
    if action.kind == "send_text":
        return action.text
    if action.kind == "confirm_cep":
        return f"Confirma que o carro fica em {action.cidade}/{action.uf}?"
    if action.kind == "ask_plan":
        return "Temos " + ", ".join(p.nome for p in action.planos) + ". Qual você quer cotar?"
    if action.kind == "present":
        quote = action.result.quote
        assert quote is not None
        return f"Plano {quote.plano_nome}: R$ {quote.premio_mensal:.2f}/mês."
    if action.kind == "refuse":
        return f"Não consigo seguir: {action.motivo}"
    if action.kind == "handoff":
        return f"Um consultor assume daqui ({action.reason.value})."
    raise ValueError(f"presenter não renderiza {action.kind}")


class FakeRules:
    """Só o que a policy usaria; o `conversation` apenas repassa este objeto."""

    def planos_resumo(self) -> list[PlanoResumo]:
        return [
            PlanoResumo(id="essencial", nome="Essencial", franquia=5000.0, coberturas=["colisao"]),
            PlanoResumo(id="completo", nome="Completo", franquia=3000.0, coberturas=["colisao", "roubo"]),
        ]


# --------------------------------------------------------------------------- infra
class FakeQuoteClient:
    """Devolve `QuoteResult`s de um roteiro e dispara `on_slow` nos que pedirem."""

    def __init__(self, resultados: list[QuoteResult], lento: bool = False) -> None:
        self.resultados = list(resultados)
        self.lento = lento
        self.pedidos: list[QuoteRequest] = []

    async def quote(
        self, req: QuoteRequest, on_slow: Callable[[], Awaitable[None]] | None = None
    ) -> QuoteResult:
        self.pedidos.append(req)
        if self.lento and on_slow is not None:
            await on_slow()
        return self.resultados.pop(0)


class FakeCepLookup:
    """`cep.lookup_cep` falso, com resposta fixa."""

    def __init__(self, info: CepInfo) -> None:
        self.info = info
        self.chamadas: list[str] = []

    async def __call__(self, cep8: str, timeout_s: float = 2.0) -> CepInfo:
        self.chamadas.append(cep8)
        return self.info


class FakeLogger:
    """JSONL com o mesmo contrato do `observability.ConversationLogger` (sem máscara de PII)."""

    def __init__(self, log_dir: Path, conversation_id: str) -> None:
        self.path = Path(log_dir) / f"{conversation_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conversation_id = conversation_id

    def event(self, event: str, message_id: str | None = None, quote_id: str | None = None, **data: Any) -> None:
        linha = {
            "conversation_id": self.conversation_id,
            "event": event,
            "message_id": message_id,
            "quote_id": quote_id,
            "data": data,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(linha, ensure_ascii=False, default=str) + "\n")

    def eventos(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(linha) for linha in self.path.read_text(encoding="utf-8").splitlines()]


def logger_factory_unico(logger: FakeLogger) -> Callable[[Path, str], FakeLogger]:
    """Mantém o mesmo logger entre turnos, para o teste ler o arquivo inteiro."""
    return lambda log_dir, conversation_id: logger


# --------------------------------------------------------------------------- dados prontos
def quote_request(**over: Any) -> QuoteRequest:
    base = {"plano_id": "completo", "idade": 35, "veiculo_ano": 2019, "cep": "01310100", "data_inicio": "2026-09-01"}
    base.update(over)
    return QuoteRequest(**base)


def quote_ok(**over: Any) -> QuoteResult:
    quote = Quote(
        plano_id="completo",
        plano_nome="Completo",
        premio_mensal=209.9,
        franquia=3000.0,
        coberturas=["colisao", "roubo", "furto"],
        multiplicadores={"faixa_etaria": 1.0, "idade_veiculo": 1.0, "regiao": 1.0},
        carencia_coberturas=["roubo", "furto"],
        carencia_dias=30,
    )
    return QuoteResult(
        quote_id="q-ok",
        outcome=QuoteOutcome.OK,
        request=quote_request(**over),
        quote=quote,
        attempts=[QuoteAttempt(attempt=1, status="ok", http_status=200, latency_ms=52)],
        total_ms=52,
    )


def quote_indisponivel(tentativas: int = 4) -> QuoteResult:
    return QuoteResult(
        quote_id="q-down",
        outcome=QuoteOutcome.INDISPONIVEL,
        request=quote_request(),
        erro="4 tentativas em 5xx",
        attempts=[
            QuoteAttempt(attempt=n, status="http_5xx", http_status=503, latency_ms=51, error="upstream_unavailable")
            for n in range(1, tentativas + 1)
        ],
        total_ms=2100,
    )


def quote_recusa(motivo: str = "idade fora da faixa aceita") -> QuoteResult:
    return QuoteResult(
        quote_id="q-recusa",
        outcome=QuoteOutcome.RECUSA,
        request=quote_request(idade=80),
        motivo_recusa=motivo,
        attempts=[QuoteAttempt(attempt=1, status="recusa", http_status=422, latency_ms=48)],
        total_ms=48,
    )
