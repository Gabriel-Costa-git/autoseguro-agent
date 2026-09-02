"""Lab do Studio: conversar com o agente REAL e ver tudo o que aconteceu no turno.

Nada aqui reimplementa o agente: cada sessão monta um `Conversation` de verdade via
`channels.cli.montar_conversa` (mesmo boot do terminal e do webhook) e só injeta dois
pontos de observação — um logger que espelha os eventos num barramento em memória e o
hook `trace` do `brain`, que publica `llm_trace` com o prompt exato enviado ao modelo.

O trace NÃO vai para o JSONL da entrega: fica só no barramento (memória, 127.0.0.1) e
por isso vai cru, sem máscara — é justamente o prompt que o operador precisa ler. O que
sai no `state` da API passa por `pii.mask_obj`, como no resto do sistema.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent.config import settings
from agent.models import Inbound, LeadState, MediaType, Outbound
from agent.observability import ConversationLogger, _to_jsonable
from agent.pii import mask_obj

MAX_HISTORICO = 500
HEARTBEAT_S = 15.0
_FIM = object()          # sentinela: sessão encerrada, o SSE pode fechar


def _log_dir() -> Path:
    """Conversas do Lab vão para `logs/studio/`, longe dos logs de entrega."""
    return Path(settings.log_dir) / "studio"


# --------------------------------------------------------------------------- barramento
class EventBus:
    """Eventos de uma sessão: histórico curto em memória + fila por assinante (SSE).

    Uma fila por assinante (e não uma por sessão) para a página poder reconectar o SSE
    sem roubar os eventos de outra aba aberta no mesmo Lab.
    """

    def __init__(self, conversation_id: str = "", maxlen: int = MAX_HISTORICO) -> None:
        self.conversation_id = conversation_id
        self._historico: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._filas: list[asyncio.Queue] = []

    def historico(self) -> list[dict[str, Any]]:
        return list(self._historico)

    def subscribe(self) -> asyncio.Queue:
        fila: asyncio.Queue = asyncio.Queue(maxsize=MAX_HISTORICO)
        self._filas.append(fila)
        return fila

    def unsubscribe(self, fila: asyncio.Queue) -> None:
        if fila in self._filas:
            self._filas.remove(fila)

    def publish(self, evento: dict[str, Any]) -> None:
        """Publica um evento já pronto. Assinante lento perde evento — nunca trava o turno."""
        self._historico.append(evento)
        for fila in list(self._filas):
            try:
                fila.put_nowait(evento)
            except asyncio.QueueFull:
                pass

    def publish_trace(self, trace: dict[str, Any]) -> None:
        """Hook do `brain`: uma chamada ao modelo (cada tentativa de retry é um evento).

        `conversation_id` é o da sessão do Lab; o `session_id` do agno (o Extractor usa
        um separado, `extract-<id>`, por não ter histórico) fica dentro de `data`.
        """
        self.publish(
            {
                "ts": datetime.now(UTC).isoformat(),
                "conversation_id": self.conversation_id,
                "event": "llm_trace",
                "message_id": None,
                "quote_id": None,
                "data": trace,
            }
        )

    def fechar(self) -> None:
        for fila in list(self._filas):
            try:
                fila.put_nowait(_FIM)
            except asyncio.QueueFull:
                pass


class LoggerDoLab:
    """`ConversationLogger` real (arquivo em `logs/studio/`) + espelho no barramento."""

    def __init__(self, log_dir: Path, conversation_id: str, bus: EventBus) -> None:
        self._real = ConversationLogger(log_dir, conversation_id)
        self._bus = bus
        self.conversation_id = conversation_id

    def event(self, event: str, message_id: str | None = None, quote_id: str | None = None, **data: Any) -> None:
        self._real.event(event, message_id, quote_id, **data)  # type: ignore[arg-type]
        self._bus.publish(
            {
                "ts": datetime.now(UTC).isoformat(),
                "conversation_id": self.conversation_id,
                "event": event,
                "message_id": message_id,
                "quote_id": quote_id,
                # mesma máscara do arquivo: o que a UI mostra é o que foi para o disco
                "data": mask_obj(_to_jsonable(data)),
            }
        )


# --------------------------------------------------------------------------- sessões
@dataclass
class Sessao:
    id: str
    api: str
    conversation: Any
    bus: EventBus
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turnos: int = 0


def resumir_state(state: LeadState | None) -> dict[str, Any]:
    """Estado enxuto e mascarado para a UI (preço só aparece se veio da API)."""
    if state is None:
        return {}
    cotacao: dict[str, Any] | None = None
    if state.quote_result is not None:
        r = state.quote_result
        cotacao = {
            "quote_id": r.quote_id,
            "outcome": r.outcome.value,
            "tentativas": len(r.attempts),
            "total_ms": r.total_ms,
            "motivo_recusa": r.motivo_recusa,
            "erro": r.erro,
            "premio_mensal": r.quote.premio_mensal if r.quote else None,
            "plano_nome": r.quote.plano_nome if r.quote else None,
        }
    dados = {
        "conversation_id": state.conversation_id,
        "stage": state.stage.value,
        "lead_nome": state.lead_nome,
        "idade": state.idade,
        "veiculo_texto": state.veiculo_texto,
        "veiculo_ano": state.veiculo_ano,
        "cep": state.cep,
        "cep_cidade": state.cep_info.cidade if state.cep_info else None,
        "cep_uf": state.cep_info.uf if state.cep_info else None,
        "cep_confirmado": state.cep_confirmado,
        "cep_ausente": state.cep_ausente,
        "plano_id": state.plano_id,
        "data_inicio": state.data_inicio.isoformat() if state.data_inicio else None,
        "ultima_pergunta": state.ultima_pergunta,
        "turnos": state.turnos,
        "turnos_sem_progresso": state.turnos_sem_progresso,
        "objecoes": state.objecoes,
        "handoff_reason": state.handoff_reason.value if state.handoff_reason else None,
        "cotacao": cotacao,
    }
    return mask_obj(dados)


class LabManager:
    """Ciclo de vida das sessões do Lab. Uma mensagem por vez por sessão (lock)."""

    def __init__(self, conversation_factory: Callable[..., Awaitable[Any]] | None = None) -> None:
        if conversation_factory is None:
            from agent.channels.cli import montar_conversa

            conversation_factory = montar_conversa
        self._factory = conversation_factory
        self._sessoes: dict[str, Sessao] = {}

    async def create(self, api_url: str | None = None) -> Sessao:
        sid = f"lab-{uuid.uuid4().hex[:8]}"
        bus = EventBus(conversation_id=sid)
        conversation = await self._factory(
            base_url=api_url,
            trace=bus.publish_trace,
            logger_factory=lambda _log_dir_ignorado, cid: LoggerDoLab(_log_dir(), cid, bus),
        )
        api = api_url or getattr(getattr(conversation, "quote_client", None), "_base_url", "") or ""
        sessao = Sessao(id=sid, api=api, conversation=conversation, bus=bus)
        self._sessoes[sid] = sessao
        return sessao

    def sessao(self, sid: str) -> Sessao:
        sessao = self._sessoes.get(sid)
        if sessao is None:
            raise KeyError(sid)
        return sessao

    async def send(self, sid: str, text: str | None = None, media_type: MediaType = "text") -> dict[str, Any]:
        sessao = self.sessao(sid)
        async with sessao.lock:
            sessao.turnos += 1
            saidas: list[Outbound] = []

            async def emit(out: Outbound) -> None:
                saidas.append(out)

            inbound = Inbound(
                conversation_id=sid,
                message_id=f"m{sessao.turnos}",
                text=text,
                media_type=media_type,
            )
            state = await sessao.conversation.handle(inbound, emit)
        return {
            "outbound": [{"message_id": o.message_id, "text": o.text, "source": o.source} for o in saidas],
            "state": resumir_state(state),
        }

    def get_state(self, sid: str) -> dict[str, Any]:
        sessao = self.sessao(sid)
        return resumir_state(sessao.conversation.store.get(sid))

    async def events(self, sid: str, heartbeat_s: float | None = None) -> AsyncIterator[dict[str, Any] | None]:
        """Histórico e, depois, os eventos novos. `None` = bata um heartbeat no SSE.

        Termina sozinho quando a sessão é encerrada (`close`), para o SSE fechar limpo.
        """
        heartbeat_s = HEARTBEAT_S if heartbeat_s is None else heartbeat_s
        sessao = self.sessao(sid)
        fila = sessao.bus.subscribe()
        historico = sessao.bus.historico()   # sem await entre subscribe e snapshot: não duplica nem perde
        try:
            for evento in historico:
                yield evento
            while True:
                try:
                    evento = await asyncio.wait_for(fila.get(), timeout=heartbeat_s)
                except TimeoutError:
                    yield None
                    continue
                if evento is _FIM:
                    return
                yield evento
        finally:
            sessao.bus.unsubscribe(fila)

    def close(self, sid: str) -> None:
        sessao = self._sessoes.pop(sid, None)
        if sessao is not None:
            sessao.bus.fechar()


# --------------------------------------------------------------------------- rotas
class SessaoIn(BaseModel):
    api: str | None = None


class MensagemIn(BaseModel):
    text: str | None = None
    media_type: MediaType = "text"


def _erro_404(sid: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"sessão desconhecida: {sid}")


def _construir_router(resolver: Callable[[Request], LabManager]) -> APIRouter:
    api = APIRouter(prefix="/api/lab", tags=["lab"])

    @api.post("/sessions")
    async def criar_sessao(request: Request, body: SessaoIn | None = None) -> dict[str, Any]:
        manager = resolver(request)
        try:
            sessao = await manager.create((body.api if body else None) or None)
        except Exception as exc:  # BootError, /planos fora, chave ausente...
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": sessao.id, "api": sessao.api}

    @api.post("/sessions/{sid}/messages")
    async def enviar(request: Request, sid: str, body: MensagemIn) -> dict[str, Any]:
        manager = resolver(request)
        try:
            return await manager.send(sid, body.text, body.media_type)
        except KeyError as exc:
            raise _erro_404(sid) from exc

    @api.get("/sessions/{sid}/state")
    async def estado(request: Request, sid: str) -> dict[str, Any]:
        manager = resolver(request)
        try:
            return manager.get_state(sid)
        except KeyError as exc:
            raise _erro_404(sid) from exc

    @api.get("/sessions/{sid}/events")
    async def eventos(request: Request, sid: str) -> StreamingResponse:
        manager = resolver(request)
        try:
            manager.sessao(sid)
        except KeyError as exc:
            raise _erro_404(sid) from exc

        async def stream() -> AsyncIterator[str]:
            async for evento in manager.events(sid):
                if evento is None:
                    yield ": ping\n\n"
                else:
                    yield f"data: {json.dumps(evento, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @api.delete("/sessions/{sid}")
    async def encerrar(request: Request, sid: str) -> dict[str, Any]:
        manager = resolver(request)
        try:
            manager.sessao(sid)
        except KeyError as exc:
            raise _erro_404(sid) from exc
        manager.close(sid)
        return {"id": sid, "closed": True}

    return api


def _do_app(request: Request) -> LabManager:
    """Manager do app: reaproveita o que estiver em `app.state`, senão cria um na 1ª chamada."""
    manager = getattr(request.app.state, "lab_manager", None)
    if manager is None:
        manager = LabManager(getattr(request.app.state, "conversation_factory", None))
        request.app.state.lab_manager = manager
    return manager


class _LabRouter(APIRouter):
    """Router pronto do Lab que também é fábrica — as duas formas de montar o Lab.

    `app.include_router(router)` (é o que o `build_studio_app` faz) monta as rotas usando
    o manager guardado em `app.state`; `router(manager)` devolve um router novo amarrado a
    um manager específico, que é como os testes injetam uma `conversation_factory`.
    Sem `prefix` próprio: os caminhos já vêm completos das rotas copiadas.
    """

    def __call__(self, manager: LabManager) -> APIRouter:  # type: ignore[override]
        return _construir_router(lambda _request: manager)


router = _LabRouter(tags=["lab"])
router.routes.extend(_construir_router(_do_app).routes)
