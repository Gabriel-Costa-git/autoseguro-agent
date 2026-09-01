"""Testes do adaptador Evolution API: parse do webhook, envio e o comportamento
do app (responde rápido, processa em background, serializa por conversation_id).
Sem rede: `httpx.MockTransport` no sender e `httpx.ASGITransport` no app; um fake
mínimo de `Conversation` (só precisa de `async handle(inbound, emit)`).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from agent.channels.evolution import EvolutionSender, build_app, parse_webhook
from agent.models import Inbound, LeadState, Outbound


def _payload(
    *,
    texto: str | None = None,
    extended: str | None = None,
    media: str | None = None,
    remote_jid: str = "5511999999999@s.whatsapp.net",
    from_me: bool = False,
    message_id: str = "3EB0ABC",
    push_name: str | None = "Fulano",
    ts: int | None = 1709550600,
    event: str = "messages.upsert",
    com_data: bool = True,
) -> dict[str, Any]:
    message: dict[str, Any] = {}
    if texto is not None:
        message["conversation"] = texto
    if extended is not None:
        message["extendedTextMessage"] = {"text": extended}
    if media is not None:
        message[f"{media}Message"] = {}

    payload: dict[str, Any] = {"event": event, "instance": "minha-instancia"}
    if com_data:
        payload["data"] = {
            "key": {"remoteJid": remote_jid, "fromMe": from_me, "id": message_id},
            "message": message,
            "messageTimestamp": ts,
            "pushName": push_name,
        }
    return payload


# --------------------------------------------------------------------------- parse_webhook
def test_parse_webhook_texto_simples():
    inbound = parse_webhook(_payload(texto="Quero cotar meu carro"))
    assert inbound is not None
    assert inbound.conversation_id == "wa-5511999999999"
    assert inbound.message_id == "3EB0ABC"
    assert inbound.text == "Quero cotar meu carro"
    assert inbound.media_type == "text"
    assert inbound.sender_name == "Fulano"


def test_parse_webhook_extended_text():
    inbound = parse_webhook(_payload(extended="Tenho um Onix 2019"))
    assert inbound is not None
    assert inbound.text == "Tenho um Onix 2019"
    assert inbound.media_type == "text"


def test_parse_webhook_audio_sem_texto():
    inbound = parse_webhook(_payload(media="audio"))
    assert inbound is not None
    assert inbound.text is None
    assert inbound.media_type == "audio"


def test_parse_webhook_imagem_e_documento():
    imagem = parse_webhook(_payload(media="image"))
    documento = parse_webhook(_payload(media="document"))
    assert imagem is not None and imagem.media_type == "image"
    assert documento is not None and documento.media_type == "document"


def test_parse_webhook_grupo_ignorado():
    payload = _payload(texto="oi", remote_jid="120363012345678901@g.us")
    assert parse_webhook(payload) is None


def test_parse_webhook_from_me_ignorado():
    payload = _payload(texto="oi", from_me=True)
    assert parse_webhook(payload) is None


def test_parse_webhook_evento_errado_ignorado():
    assert parse_webhook({"event": "connection.update", "data": {}}) is None


def test_parse_webhook_sem_data_ignorado():
    assert parse_webhook({"event": "messages.upsert"}) is None
    assert parse_webhook(_payload(com_data=False)) is None
    assert parse_webhook({"event": "messages.upsert", "data": None}) is None


# --------------------------------------------------------------------------- EvolutionSender
@pytest.mark.asyncio
async def test_sender_send_text_manda_number_e_apikey():
    recebido = {}

    def handler(request: httpx.Request) -> httpx.Response:
        recebido["path"] = request.url.path
        recebido["apikey"] = request.headers.get("apikey")
        recebido["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sender = EvolutionSender("http://evolution.test", "minha-chave", "minha-instancia", client=client)

    await sender.send_text("5511999999999", "oi, tudo bem?")

    assert recebido["path"] == "/message/sendText/minha-instancia"
    assert recebido["apikey"] == "minha-chave"
    assert recebido["body"] == {"number": "5511999999999", "text": "oi, tudo bem?"}


@pytest.mark.asyncio
async def test_sender_typing_manda_presence_composing():
    recebido = {}

    def handler(request: httpx.Request) -> httpx.Response:
        recebido["path"] = request.url.path
        recebido["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sender = EvolutionSender("http://evolution.test", "chave", "minha-instancia", client=client)

    await sender.typing("5511999999999")

    assert recebido["path"] == "/chat/sendPresence/minha-instancia"
    assert recebido["body"] == {"number": "5511999999999", "presence": "composing", "delay": 1200}


@pytest.mark.asyncio
async def test_sender_falha_nao_levanta():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sender = EvolutionSender("http://evolution.test", "chave", "minha-instancia", client=client)

    await sender.send_text("5511999999999", "oi")  # não deve levantar


# --------------------------------------------------------------------------- app / build_app
class FakeConversation:
    """`Conversation` falsa: registra o que chegou, emite 1 Outbound, opcionalmente
    dorme um pouco para o teste de serialização observar a ordem."""

    def __init__(self, delay_para: str | None = None, delay_s: float = 0.05) -> None:
        self.recebidos: list[Inbound] = []
        self.ordem: list[tuple[str, str]] = []
        self.delay_para = delay_para
        self.delay_s = delay_s
        self.evento_iniciou = asyncio.Event()

    async def handle(self, inbound: Inbound, emit) -> LeadState:
        self.ordem.append(("start", inbound.message_id))
        if inbound.message_id == self.delay_para:
            self.evento_iniciou.set()
            await asyncio.sleep(self.delay_s)
        await emit(
            Outbound(
                conversation_id=inbound.conversation_id,
                message_id=f"{inbound.message_id}-o1",
                text=f"resposta para {inbound.message_id}",
                in_reply_to=inbound.message_id,
                source="template",
            )
        )
        self.ordem.append(("end", inbound.message_id))
        self.recebidos.append(inbound)
        return LeadState(conversation_id=inbound.conversation_id)


def _sender_mudo() -> tuple[EvolutionSender, list[dict[str, Any]]]:
    """Sender com transporte mock que só registra as chamadas, sempre responde 200."""
    chamadas: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas.append({"path": request.url.path, "body": json.loads(request.content)})
        return httpx.Response(200, json={"status": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return EvolutionSender("http://evolution.test", "chave", "minha-instancia", client=client), chamadas


@pytest.mark.asyncio
async def test_health():
    sender, _ = _sender_mudo()
    app = build_app(FakeConversation(), sender)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_webhook_evento_ignorado_nao_chama_handle():
    sender, _ = _sender_mudo()
    conversation = FakeConversation()
    app = build_app(conversation, sender)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/webhook", json={"event": "connection.update", "data": {}})
    assert resp.status_code == 200
    assert resp.json() == {"ignored": True}
    assert conversation.recebidos == []


@pytest.mark.asyncio
async def test_webhook_chama_handle_e_sender_recebe_sendtext_com_numero_certo():
    sender, chamadas = _sender_mudo()
    conversation = FakeConversation()
    app = build_app(conversation, sender)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/webhook", json=_payload(texto="quero cotar"))

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert len(conversation.recebidos) == 1
    assert conversation.recebidos[0].text == "quero cotar"

    send_text_calls = [c for c in chamadas if c["path"].startswith("/message/sendText/")]
    typing_calls = [c for c in chamadas if c["path"].startswith("/chat/sendPresence/")]
    assert send_text_calls == [
        {
            "path": "/message/sendText/minha-instancia",
            "body": {"number": "5511999999999", "text": "resposta para 3EB0ABC"},
        }
    ]
    assert len(typing_calls) == 1


@pytest.mark.asyncio
async def test_webhook_falha_do_sender_nao_derruba_processamento():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("evolution fora do ar", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sender = EvolutionSender("http://evolution.test", "chave", "minha-instancia", client=client)
    conversation = FakeConversation()
    app = build_app(conversation, sender)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client_app:
        resp = await client_app.post("/webhook", json=_payload(texto="oi"))

    assert resp.status_code == 200
    assert len(conversation.recebidos) == 1


@pytest.mark.asyncio
async def test_duas_mensagens_do_mesmo_lead_sao_serializadas_em_ordem():
    sender, _ = _sender_mudo()
    conversation = FakeConversation(delay_para="m1", delay_s=0.05)
    app = build_app(conversation, sender)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        task1 = asyncio.create_task(client.post("/webhook", json=_payload(texto="primeira", message_id="m1")))
        await conversation.evento_iniciou.wait()  # m1 já está dentro do lock quando m2 chega
        task2 = asyncio.create_task(client.post("/webhook", json=_payload(texto="segunda", message_id="m2")))
        resp1, resp2 = await asyncio.gather(task1, task2)

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert conversation.ordem == [("start", "m1"), ("end", "m1"), ("start", "m2"), ("end", "m2")]
