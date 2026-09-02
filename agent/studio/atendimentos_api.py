"""Rotas de Atendimentos do Studio: ver as conversas reais e assumir uma delas.

Leitura vem do `Catalogo` (`agent/atendimentos.py`, que lê os JSONL) e o interruptor
"quem responde" é o `TakeoverStore` (`agent/takeover.py`) — os dois vivem fora de
`agent/studio/` de propósito: o canal (`agent/serve.py`) precisa do takeover e nunca
pode importar o Studio.

Emenda aprovada da fase F6: o Studio ENVIA pela Evolution quando o operador assume a
conversa (`POST .../mensagens`), mas continua sem receber webhook. Por isso o
`EvolutionSender` é resolvido de `app.state` (o teste injeta um falso) e, na falta dele,
montado das settings — se a Evolution não estiver configurada, a rota devolve 400 em vez
de fingir que enviou.

Este módulo não decide nada do agente: lê log, marca/desmarca takeover e registra o que
o humano enviou como um `outbound` com `source="humano"` no mesmo log da conversa.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from agent.atendimentos import Catalogo
from agent.channels.evolution import EvolutionSender, _number_from_conversation_id
from agent.config import settings
from agent.observability import ConversationLogger
from agent.takeover import TakeoverStore

PREFIXO_WHATSAPP = "wa-"


class MensagemIn(BaseModel):
    text: str | None = None


def _catalogo(request: Request) -> Catalogo:
    return request.app.state.catalogo


def _takeover(request: Request) -> TakeoverStore:
    return request.app.state.takeover


def _sender(request: Request) -> Any:
    """Sender injetado em `app.state.evolution_sender` ou montado das settings."""
    injetado = getattr(request.app.state, "evolution_sender", None)
    if injetado is not None:
        return injetado
    if not (settings.evolution_url and settings.evolution_apikey and settings.evolution_instance):
        raise HTTPException(
            status_code=400,
            detail="Evolution API não configurada (EVOLUTION_URL/APIKEY/INSTANCE): não dá para enviar pelo WhatsApp.",
        )
    return EvolutionSender(settings.evolution_url, settings.evolution_apikey, settings.evolution_instance)


def _resumo_ou_404(request: Request, cid: str) -> dict[str, Any]:
    try:
        return _catalogo(request).resumo(cid)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"atendimento desconhecido: {cid}") from exc


def construir_router() -> APIRouter:
    api = APIRouter(prefix="/api/atendimentos", tags=["atendimentos"])

    @api.get("")
    def listar(
        request: Request, origem: str | None = None, status: str | None = None, q: str | None = None
    ) -> dict[str, Any]:
        return {"itens": _catalogo(request).listar(origem=origem, status=status, q=q)}

    @api.get("/{cid}")
    def detalhe(request: Request, cid: str, since: int = 0) -> dict[str, Any]:
        try:
            return _catalogo(request).transcricao(cid, since=since)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"atendimento desconhecido: {cid}") from exc

    @api.post("/{cid}/assumir")
    def assumir(request: Request, cid: str) -> dict[str, Any]:
        _resumo_ou_404(request, cid)
        _takeover(request).assumir(cid)
        return _resumo_ou_404(request, cid)

    @api.post("/{cid}/devolver")
    def devolver(request: Request, cid: str) -> dict[str, Any]:
        _resumo_ou_404(request, cid)
        _takeover(request).devolver(cid)
        return _resumo_ou_404(request, cid)

    @api.post("/{cid}/mensagens")
    async def enviar(request: Request, cid: str, body: MensagemIn) -> dict[str, bool]:
        _resumo_ou_404(request, cid)
        texto = (body.text or "").strip()
        if not texto:
            raise HTTPException(status_code=400, detail="texto vazio")
        if not cid.startswith(PREFIXO_WHATSAPP):
            raise HTTPException(status_code=400, detail=f"só dá para enviar em conversas {PREFIXO_WHATSAPP}*")
        if not _takeover(request).is_humano(cid):
            raise HTTPException(status_code=400, detail="assuma a conversa antes de enviar mensagem")

        sender = _sender(request)
        await sender.send_text(_number_from_conversation_id(cid), texto)
        ConversationLogger(_catalogo(request).log_dir, cid).event(
            "outbound",
            message_id=f"h-{uuid.uuid4().hex[:8]}",
            text=texto,
            source="humano",
        )
        return {"ok": True}

    return api


router = construir_router()
