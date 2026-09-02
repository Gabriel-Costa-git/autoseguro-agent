"""Adaptador do canal WhatsApp via Evolution API v2.

O webhook (`POST /webhook`) recebe `messages.upsert`, converte para `Inbound` e
despacha pro `Conversation.handle` só DEPOIS de responder 200 — a Evolution não
pode esperar o turno inteiro (LLM + API de cotação instável) sob pena de re-
entregar o webhook por timeout. Um `asyncio.Lock` por `conversation_id` garante
que duas mensagens seguidas do mesmo lead sejam processadas em sequência, nunca
concorrentes (senão a `Conversation` leria/gravaria o mesmo `LeadState` em paralelo).

`EvolutionSender` é o lado de saída: manda texto e o indicador "digitando". Erro
de envio vira log, nunca exceção — um WhatsApp fora do ar não pode derrubar o
processamento do lead (o estado já avançou; só a entrega falhou).

`takeover` (opcional) é o interruptor "quem responde este lead": quando o operador
assume a conversa no Studio, a mensagem que chega vira só um evento `inbound` com
`modo="humano"` no log e o agente NÃO é chamado. Sem `takeover` (padrão), o
comportamento é exatamente o entregue.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, Request

from agent.config import settings
from agent.models import Inbound, MediaType, Outbound
from agent.observability import ConversationLogger

log = logging.getLogger("autoseguro.evolution")

_DIGITS_RE = re.compile(r"\d+")


def _only_digits(texto: str) -> str:
    return "".join(_DIGITS_RE.findall(texto))


def _media_type(message: dict[str, Any]) -> MediaType:
    if "audioMessage" in message:
        return "audio"
    if "imageMessage" in message:
        return "image"
    if "documentMessage" in message:
        return "document"
    if "conversation" in message or "extendedTextMessage" in message:
        return "text"
    return "other"


def _texto(message: dict[str, Any]) -> str | None:
    if "conversation" in message:
        return message["conversation"]
    extended = message.get("extendedTextMessage")
    if isinstance(extended, dict):
        return extended.get("text")
    return None


def _origem(payload: dict[str, Any]) -> str:
    """`whatsapp:<instância>`: a instância do payload (uma Evolution serve várias) ou a do `.env`."""
    instancia = payload.get("instance") or settings.evolution_instance
    return f"whatsapp:{instancia}" if instancia else "whatsapp"


def parse_webhook(payload: dict[str, Any]) -> Inbound | None:
    """`None` para o que não vira turno: evento diferente, grupo, eco do próprio agente, sem `data`."""
    if payload.get("event") != "messages.upsert":
        return None
    data = payload.get("data")
    if not data:
        return None

    key = data.get("key") or {}
    if key.get("fromMe"):
        return None

    remote_jid = key.get("remoteJid") or ""
    if remote_jid.endswith("@g.us"):
        return None
    digits = _only_digits(remote_jid)
    if not digits:
        return None

    message = data.get("message") or {}
    ts = data.get("messageTimestamp")

    return Inbound(
        conversation_id=f"wa-{digits}",
        message_id=key.get("id") or "",
        text=_texto(message),
        media_type=_media_type(message),
        sender_name=data.get("pushName"),
        origem=_origem(payload),
        ts=datetime.fromtimestamp(int(ts), tz=UTC) if ts is not None else datetime.now(UTC),
    )


class EvolutionSender:
    """Lado de saída: `POST /message/sendText` e `POST /chat/sendPresence`. Nunca levanta."""

    def __init__(
        self,
        base_url: str,
        apikey: str,
        instance: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._apikey = apikey
        self._instance = instance
        self._client = client

    async def _post(self, path: str, body: dict[str, Any]) -> None:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient()
        try:
            resp = await client.post(
                f"{self._base_url}{path}",
                json=body,
                headers={"apikey": self._apikey},
                timeout=10.0,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("falha ao chamar Evolution API (%s): %s", path, type(exc).__name__)
        finally:
            if owns_client:
                await client.aclose()

    async def send_text(self, number: str, text: str) -> None:
        await self._post(f"/message/sendText/{self._instance}", {"number": number, "text": text})

    async def typing(self, number: str) -> None:
        await self._post(
            f"/chat/sendPresence/{self._instance}",
            {"number": number, "presence": "composing", "delay": 1200},
        )


def _number_from_conversation_id(conversation_id: str) -> str:
    return conversation_id.removeprefix("wa-")


def _logar_modo_humano(inbound: Inbound) -> None:
    """Conversa assumida por um humano: a mensagem entra no log e para por aí."""
    ConversationLogger(settings.log_dir, inbound.conversation_id).event(
        "inbound",
        message_id=inbound.message_id,
        text=inbound.text,
        media_type=inbound.media_type,
        sender_name=inbound.sender_name,
        origem=inbound.origem,
        modo="humano",
    )


def build_app(conversation: Any, sender: EvolutionSender, takeover: Any = None) -> FastAPI:
    """`conversation` só precisa expor `async handle(inbound, emit)` (o Protocol de `Conversation`).

    `takeover` só precisa expor `is_humano(conversation_id) -> bool` (`agent.takeover.TakeoverStore`).
    """
    app = FastAPI(title="AutoSeguro — webhook Evolution")
    locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _processar(inbound: Inbound) -> None:
        async def emit(out: Outbound) -> None:
            number = _number_from_conversation_id(out.conversation_id)
            await sender.typing(number)
            await sender.send_text(number, out.text)

        async with locks[inbound.conversation_id]:
            # A checagem fica DENTRO do lock: assumir/devolver no meio de um turno em voo
            # não pode fazer o agente e o operador responderem a mesma mensagem.
            if takeover is not None and takeover.is_humano(inbound.conversation_id):
                _logar_modo_humano(inbound)
                return
            await conversation.handle(inbound, emit)

    @app.post("/webhook")
    async def webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, bool]:
        payload = await request.json()
        inbound = parse_webhook(payload)
        if inbound is None:
            return {"ignored": True}
        background_tasks.add_task(_processar, inbound)
        return {"ok": True}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


