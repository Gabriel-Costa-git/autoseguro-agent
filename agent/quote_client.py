"""Cliente resiliente para a API de cotação (`POST /quote`, `GET /planos`).

A API falha de propósito: 20% das chamadas devolvem 5xx (`upstream_unavailable`)
e 10% dormem 8s antes de responder normal. `POST /quote` é pura e idempotente,
então retry é seguro; erros de negócio (422 `cotacao_recusada`) e bugs nossos
(422 `detail` / 400 `payload_invalido`) NUNCA são retentados — não adianta
tentar de novo uma recusa ou um payload malformado.

Matemática do retry (`max_attempts=4`, cada tentativa independente com 20% de
falha de infra): P(as 4 esgotarem por infra) = 0.3^4 ≈ 0.8% — timeout entra
nessa conta como falha (é a fração "lenta" que também vira retry). Pior caso
de tempo, sem cortar por orçamento: 4 tentativas × timeout_s (3.5s) mais os
backoffs entre elas; `budget_s` (15s) é o teto duro que interrompe a série
antes de estourar esse pior caso.
"""
from __future__ import annotations

import asyncio
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

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
    """GET /planos falhou (rede/timeout/status != 200). O chamador decide o que fazer."""


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
        self._planos_cache: dict | None = None

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

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._base_url, transport=self._transport)

    async def get_planos(self) -> dict:
        """GET /planos, cacheado em memória após o 1º sucesso (a API é estável, não precisa retry)."""
        if self._planos_cache is not None:
            return self._planos_cache
        timeout_s, _, _, _ = self._cfg()
        async with self._new_client() as client:
            try:
                resp = await client.get("/planos", timeout=timeout_s)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise QuotePlanosUnavailable(_short_error(exc)) from exc
        if resp.status_code != 200:
            raise QuotePlanosUnavailable(f"GET /planos -> {resp.status_code}")
        self._planos_cache = resp.json()
        return self._planos_cache

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
