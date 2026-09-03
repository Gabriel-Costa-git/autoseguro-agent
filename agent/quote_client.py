"""Cliente resiliente para a API de cotação (`POST /quote`, `GET /planos`).

A API falha de propósito: 20% das chamadas devolvem 5xx (`upstream_unavailable`)
e 10% dormem 8s antes de responder normal. `POST /quote` é pura e idempotente,
então retry é seguro; erros de negócio (422 `cotacao_recusada`) e bugs nossos
(422 `detail` / 400 `payload_invalido`) NUNCA são retentados — não adianta
tentar de novo uma recusa ou um payload malformado.

Matemática do retry (cada tentativa independente com 30% de chance de falhar
por infra — 20% de 5xx mais os 10% lentos que estouram o timeout): com 3
tentativas, P(esgotar) = 0.3³ ≈ 2,7%; com 4, 0.3⁴ ≈ 0,8%. Na prática, com a
API 100% lenta a série para em 3: depois de 3 timeouts o relógio já passou de
11,25s e a 4ª exigiria ≥ 15,75s, acima do `budget_s` de 15s. `max_attempts=4`
é o teto de tentativas; `budget_s` é o teto de tempo, e é ele que manda quando
todas as respostas são lentas.

`GET /planos` usa o MESMO retry: as regras de aceitação (idade, ano do veículo)
mudam com o tempo e com o calendário, então o catálogo é recarregado a cada
`planos_ttl_s`. Falhou depois de já ter uma cópia boa? Devolve a cópia marcada
`stale` — errar a data de validade das regras é muito menos grave que derrubar
a conversa. `QuotePlanosUnavailable` só sobe quando não há cópia nenhuma, que é
o boot (fail-fast do CLI/serve).
"""
from __future__ import annotations

import asyncio
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from agent.models import (
    ProRata,
    Quote,
    QuoteAttempt,
    QuoteOutcome,
    QuoteRequest,
    QuoteResult,
)


class QuotePlanosUnavailable(Exception):
    """GET /planos falhou e não há cópia em memória. O chamador decide o que fazer."""


PlanosOrigem = Literal["http", "cache", "stale"]


@dataclass(frozen=True)
class PlanosSnapshot:
    """De onde veio o catálogo desta leitura — é o que vira o evento `planos_refresh`."""

    planos: dict
    origem: PlanosOrigem
    obtido_em: float            # leitura do `clock` em que a cópia BOA foi buscada
    idade_s: float              # há quanto tempo essa cópia foi buscada
    erro: str | None = None     # preenchido só em `stale`
    latency_ms: int = 0         # tempo do GET desta leitura; 0 quando veio do cache

    @property
    def stale(self) -> bool:
        return self.origem == "stale"


class QuoteClient:
    def __init__(
        self,
        base_url: str,
        timeout_s: float = 3.5,
        max_attempts: int = 4,
        budget_s: float = 15.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        backoff_base_s: float = 0.5,
        rng: random.Random | None = None,
        params: Callable[[], dict] | None = None,
        planos_ttl_s: float = 90.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._max_attempts = max_attempts
        self._budget_s = budget_s
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._backoff_base_s = backoff_base_s
        self._rng = rng or random.Random()
        self._params = params
        self._planos_ttl_s = planos_ttl_s
        self._planos_cache: dict | None = None
        self._planos_obtido_em: float | None = None
        self.ultimo_planos: PlanosSnapshot | None = None   # lido pelo `conversation` para o log

    def _cfg(self) -> tuple[float, int, float, float]:
        """timeout, tentativas, orçamento e backoff desta chamada.

        Com `params` (o Studio passa um lambda que lê o store), os quatro são relidos a
        cada chamada — mudar o timeout na UI vale na cotação seguinte, sem reiniciar.
        `base_url` fica fixo por instância: trocar de API é criar outro client.
        """
        if self._params is None:
            return self._timeout_s, self._max_attempts, self._budget_s, self._backoff_base_s
        p = self._params() or {}
        return (
            float(p.get("timeout_s", self._timeout_s)),
            int(p.get("max_attempts", self._max_attempts)),
            float(p.get("budget_s", self._budget_s)),
            float(p.get("backoff_base_s", self._backoff_base_s)),
        )

    def _ttl(self) -> float:
        """TTL do catálogo, relido a cada chamada (o Studio muda em tempo real)."""
        if self._params is None:
            return self._planos_ttl_s
        return float((self._params() or {}).get("planos_ttl_s", self._planos_ttl_s))

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._base_url, transport=self._transport)

    async def get_planos(self) -> dict:
        """O catálogo corrente. Levanta `QuotePlanosUnavailable` só quando não há cópia nenhuma."""
        return (await self.planos()).planos

    async def planos(self) -> PlanosSnapshot:
        """Catálogo com TTL: dentro do prazo devolve a cópia, fora dele tenta a API.

        Falha com cópia em mãos vira `stale` (a conversa segue com regras de minutos atrás);
        falha sem cópia levanta — é o boot, e aí a mensagem clara vale mais que um agente que
        acha que sabe as regras.
        """
        agora = self._clock()
        if self._planos_cache is not None and self._planos_obtido_em is not None:
            idade = agora - self._planos_obtido_em
            if idade < self._ttl():
                return self._registrar(PlanosSnapshot(self._planos_cache, "cache", self._planos_obtido_em, idade))

        inicio = time.perf_counter()
        try:
            planos = await self._buscar_planos()
        except QuotePlanosUnavailable as exc:
            if self._planos_cache is None or self._planos_obtido_em is None:
                raise
            return self._registrar(
                PlanosSnapshot(
                    self._planos_cache,
                    "stale",
                    self._planos_obtido_em,
                    self._clock() - self._planos_obtido_em,
                    erro=str(exc)[:200],
                    latency_ms=int((time.perf_counter() - inicio) * 1000),
                )
            )

        self._planos_cache = planos
        self._planos_obtido_em = self._clock()
        return self._registrar(
            PlanosSnapshot(
                planos, "http", self._planos_obtido_em, 0.0,
                latency_ms=int((time.perf_counter() - inicio) * 1000),
            )
        )

    def _registrar(self, snapshot: PlanosSnapshot) -> PlanosSnapshot:
        self.ultimo_planos = snapshot
        return snapshot

    async def _buscar_planos(self) -> dict:
        """GET /planos com o MESMO retry/backoff/orçamento do `quote()`.

        5xx e timeout são retentados (a API cai de propósito); 4xx não melhora tentando de novo.
        """
        timeout_s, max_attempts, budget_s, backoff_base_s = self._cfg()
        start = self._clock()
        erro = "sem tentativas"
        async with self._new_client() as client:
            n = 1
            while True:
                try:
                    resp = await client.get("/planos", timeout=timeout_s)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    erro = _short_error(exc)
                else:
                    if resp.status_code == 200:
                        return resp.json()
                    erro = f"GET /planos -> {resp.status_code}"
                    if not 500 <= resp.status_code < 600:
                        break
                if n >= max_attempts:
                    break
                sleep_s = self._backoff_seconds(n, backoff_base_s)
                if (self._clock() - start) + sleep_s + timeout_s > budget_s:
                    break
                await self._sleep(sleep_s)
                n += 1
        raise QuotePlanosUnavailable(erro)

    async def quote(
        self,
        req: QuoteRequest,
        on_slow: Callable[[], Awaitable[None]] | None = None,
    ) -> QuoteResult:
        quote_id = "q" + uuid.uuid4().hex[:8]  # prefixo: id só de dígitos seria mascarado como CEP no log
        timeout_s, max_attempts, budget_s, backoff_base_s = self._cfg()
        payload = req.model_dump(exclude_none=True)
        attempts: list[QuoteAttempt] = []
        start = self._clock()
        slow_notified = False

        async with self._new_client() as client:
            n = 1
            while True:
                attempt_start = self._clock()
                try:
                    resp = await client.post("/quote", json=payload, timeout=timeout_s)
                except httpx.TimeoutException as exc:
                    latency_ms = _ms(self._clock() - attempt_start)
                    attempts.append(
                        QuoteAttempt(attempt=n, status="timeout", latency_ms=latency_ms, error=_short_error(exc))
                    )
                    if not slow_notified and on_slow is not None:
                        slow_notified = True
                        await on_slow()
                except httpx.TransportError as exc:
                    latency_ms = _ms(self._clock() - attempt_start)
                    attempts.append(
                        QuoteAttempt(attempt=n, status="erro_rede", latency_ms=latency_ms, error=_short_error(exc))
                    )
                else:
                    latency_ms = _ms(self._clock() - attempt_start)
                    terminal = self._classify_response(n, resp, latency_ms, attempts)
                    if terminal is not None:
                        outcome, quote, motivo_recusa, erro = terminal
                        return QuoteResult(
                            quote_id=quote_id,
                            outcome=outcome,
                            request=req,
                            quote=quote,
                            motivo_recusa=motivo_recusa,
                            erro=erro,
                            attempts=attempts,
                            total_ms=_ms(self._clock() - start),
                        )

                if n >= max_attempts:
                    break

                sleep_s = self._backoff_seconds(n, backoff_base_s)
                elapsed = self._clock() - start
                if elapsed + sleep_s + timeout_s > budget_s:
                    break

                await self._sleep(sleep_s)
                n += 1

        erro = attempts[-1].error if attempts else "sem tentativas"
        return QuoteResult(
            quote_id=quote_id,
            outcome=QuoteOutcome.INDISPONIVEL,
            request=req,
            erro=erro,
            attempts=attempts,
            total_ms=_ms(self._clock() - start),
        )

    def _classify_response(
        self, n: int, resp: httpx.Response, latency_ms: int, attempts: list[QuoteAttempt]
    ) -> tuple[QuoteOutcome, Quote | None, str | None, str | None] | None:
        """Classifica a resposta HTTP; devolve (outcome, quote, motivo_recusa, erro) se terminal, senão None (retry)."""
        status = resp.status_code

        if status == 200:
            quote = _parse_quote(resp.json())
            attempts.append(QuoteAttempt(attempt=n, status="ok", http_status=status, latency_ms=latency_ms))
            return QuoteOutcome.OK, quote, None, None

        body = _safe_json(resp)

        if status == 422 and isinstance(body, dict) and body.get("error") == "cotacao_recusada":
            motivo = body.get("motivo") or "Cotação recusada."
            attempts.append(
                QuoteAttempt(attempt=n, status="recusa", http_status=status, latency_ms=latency_ms, error=motivo)
            )
            return QuoteOutcome.RECUSA, None, motivo, None

        if status == 422 or status == 400:
            erro = _resumo_bug(status, body)
            attempts.append(
                QuoteAttempt(attempt=n, status="bug", http_status=status, latency_ms=latency_ms, error=erro)
            )
            return QuoteOutcome.BUG, None, None, erro

        if 500 <= status < 600:
            erro = _resumo_5xx(status, body)
            attempts.append(
                QuoteAttempt(attempt=n, status="http_5xx", http_status=status, latency_ms=latency_ms, error=erro)
            )
            return None  # retry

        erro = f"status inesperado {status}"
        attempts.append(QuoteAttempt(attempt=n, status="bug", http_status=status, latency_ms=latency_ms, error=erro))
        return QuoteOutcome.BUG, None, None, erro

    def _backoff_seconds(self, tentativa_falha: int, base_s: float | None = None) -> float:
        """`base * 2**(n-1)` com jitter uniforme ±50%."""
        base = (self._backoff_base_s if base_s is None else base_s) * (2 ** (tentativa_falha - 1))
        jitter = self._rng.uniform(-0.5, 0.5) * base
        return max(0.0, base + jitter)


def _ms(seconds: float) -> int:
    return int(seconds * 1000)


def _short_error(exc: Exception) -> str:
    msg = str(exc) or exc.__class__.__name__
    return msg[:200]


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _resumo_bug(status_code: int, body: Any) -> str:
    if status_code == 400:
        detalhe = body.get("detalhe") if isinstance(body, dict) else None
        return f"payload_invalido: {detalhe}"[:200] if detalhe else "payload_invalido"
    if isinstance(body, dict) and "detail" in body:
        return f"validacao: {body['detail']}"[:200]
    return f"erro {status_code}"


def _resumo_5xx(status_code: int, body: Any) -> str:
    if isinstance(body, dict) and body.get("error"):
        return f"{status_code} {body['error']}"
    return f"http {status_code}"


def _parse_quote(body: dict) -> Quote:
    carencia = body.get("carencia") or {}
    pro_rata_body = body.get("primeiro_pagamento_pro_rata")
    pro_rata = ProRata(**pro_rata_body) if pro_rata_body else None
    return Quote(
        plano_id=body["plano_id"],
        plano_nome=body["plano_nome"],
        premio_mensal=body["premio_mensal"],
        franquia=body["franquia"],
        coberturas=body["coberturas"],
        multiplicadores=body["multiplicadores"],
        carencia_coberturas=carencia.get("coberturas", []),
        carencia_dias=carencia.get("dias", 0),
        carencia_observacao=carencia.get("observacao"),
        moeda=body.get("moeda", "BRL"),
        pro_rata=pro_rata,
    )
