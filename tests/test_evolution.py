"""Testes do adaptador Evolution API: parse do webhook, envio e o comportamento
do app (responde rápido, processa em background, serializa por conversation_id).
Sem rede: `httpx.MockTransport` no sender e `httpx.ASGITransport` no app; um fake
mínimo de `Conversation` (só precisa de `async handle(inbound, emit)`).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from agent.channels import evolution as evolution_mod
from agent.channels.evolution import EvolutionSender, build_app, parse_webhook
from agent.models import Inbound, LeadState, Outbound
from agent.pii import nome_arquivo_log
from agent.takeover import TakeoverStore


class ConfigFake:
    """Config do canal sem tocar `config/`: debounce desligado, salvo quando o teste quer."""

    def __init__(self, **over: Any) -> None:
        self.valores = {"max_respostas_por_minuto": 6, "debounce_s": 0.0, **over}

    def param(self, path: str) -> Any:
        grupo, _, chave = path.rpartition(".")
        assert grupo == "tools.canal", grupo
        return self.valores[chave]


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
    message: dict[str, Any] | None = None,
    stub_type: int | None = None,
) -> dict[str, Any]:
    if message is None:
        message = {}
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
        if stub_type is not None:
            payload["data"]["messageStubType"] = stub_type
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


def test_parse_webhook_origem_vem_da_instancia_do_payload():
    """Uma Evolution serve várias instâncias: a origem tem de dizer QUAL atendeu o lead."""
    inbound = parse_webhook(_payload(texto="oi"))
    assert inbound is not None
    assert inbound.origem == "whatsapp:minha-instancia"


def test_parse_webhook_origem_cai_para_a_instancia_do_env(monkeypatch):
    monkeypatch.setattr(evolution_mod, "settings", SimpleNamespace(evolution_instance="do-env"))
    payload = _payload(texto="oi")
    del payload["instance"]
    inbound = parse_webhook(payload)
    assert inbound is not None
    assert inbound.origem == "whatsapp:do-env"


def test_parse_webhook_sem_instancia_em_lugar_nenhum(monkeypatch):
    monkeypatch.setattr(evolution_mod, "settings", SimpleNamespace(evolution_instance=None))
    payload = _payload(texto="oi")
    del payload["instance"]
    inbound = parse_webhook(payload)
    assert inbound is not None
    assert inbound.origem == "whatsapp"


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


def test_parse_webhook_mensagem_vazia_ignorada():
    """Upsert com `message` vazio não é mensagem de ninguém — era 1 turno por recibo."""
    assert parse_webhook(_payload(message={})) is None
    assert parse_webhook(_payload(message=None, texto=None)) is None


@pytest.mark.parametrize(
    "chave",
    [
        "protocolMessage",
        "senderKeyDistributionMessage",
        "messageContextInfo",
        "reactionMessage",
        "editedMessage",
        "pollUpdateMessage",
    ],
)
def test_parse_webhook_so_com_chave_de_protocolo_ignorado(chave):
    assert parse_webhook(_payload(message={chave: {"algo": 1}})) is None


def test_parse_webhook_protocolo_junto_com_texto_vira_turno():
    """`messageContextInfo` acompanha mensagem de verdade: o que decide é o CONTEÚDO."""
    inbound = parse_webhook(_payload(message={"messageContextInfo": {}, "conversation": "oi"}))
    assert inbound is not None and inbound.text == "oi"


def test_parse_webhook_message_stub_type_ignorado():
    """Evento de sistema da conversa (chamada perdida, número mudou), não mensagem."""
    assert parse_webhook(_payload(texto="oi", stub_type=2)) is None


def test_parse_webhook_ephemeral_vazio_ignorado_e_com_texto_aceito():
    vazio = _payload(message={"ephemeralMessage": {"message": {"protocolMessage": {}}}})
    assert parse_webhook(vazio) is None

    com_texto = _payload(message={"ephemeralMessage": {"message": {"conversation": "some em 24h"}}})
    inbound = parse_webhook(com_texto)
    assert inbound is not None and inbound.text == "some em 24h"


def test_parse_webhook_midia_continua_virando_turno():
    """O filtro é de protocolo, não de mídia: áudio/imagem/documento seguem valendo."""
    for media, esperado in (("audio", "audio"), ("image", "image"), ("document", "document")):
        inbound = parse_webhook(_payload(media=media))
        assert inbound is not None and inbound.media_type == esperado
    sticker = parse_webhook(_payload(message={"stickerMessage": {}}))
    assert sticker is not None and sticker.media_type == "other"


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
    app = build_app(FakeConversation(), sender, config=ConfigFake())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_webhook_evento_ignorado_nao_chama_handle():
    sender, _ = _sender_mudo()
    conversation = FakeConversation()
    app = build_app(conversation, sender, config=ConfigFake())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/webhook", json={"event": "connection.update", "data": {}})
    assert resp.status_code == 200
    assert resp.json() == {"ignored": True}
    assert conversation.recebidos == []


@pytest.mark.asyncio
async def test_webhook_chama_handle_e_sender_recebe_sendtext_com_numero_certo():
    sender, chamadas = _sender_mudo()
    conversation = FakeConversation()
    app = build_app(conversation, sender, config=ConfigFake())

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
    # "digitando" ao ENTRAR no turno e de novo antes da saída: o lead vê atividade
    # durante a extração e a cotação, não só no instante do envio.
    assert len(typing_calls) == 2


@pytest.mark.asyncio
async def test_webhook_falha_do_sender_nao_derruba_processamento():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("evolution fora do ar", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sender = EvolutionSender("http://evolution.test", "chave", "minha-instancia", client=client)
    conversation = FakeConversation()
    app = build_app(conversation, sender, config=ConfigFake())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client_app:
        resp = await client_app.post("/webhook", json=_payload(texto="oi"))

    assert resp.status_code == 200
    assert len(conversation.recebidos) == 1


@pytest.mark.asyncio
async def test_duas_mensagens_do_mesmo_lead_sao_serializadas_em_ordem():
    sender, _ = _sender_mudo()
    conversation = FakeConversation(delay_para="m1", delay_s=0.05)
    app = build_app(conversation, sender, config=ConfigFake())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        task1 = asyncio.create_task(client.post("/webhook", json=_payload(texto="primeira", message_id="m1")))
        await conversation.evento_iniciou.wait()  # m1 já está dentro do lock quando m2 chega
        task2 = asyncio.create_task(client.post("/webhook", json=_payload(texto="segunda", message_id="m2")))
        resp1, resp2 = await asyncio.gather(task1, task2)

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert conversation.ordem == [("start", "m1"), ("end", "m1"), ("start", "m2"), ("end", "m2")]


# --------------------------------------------------------------------------- takeover
def _com_takeover(tmp_path, monkeypatch, assumidas: list[str]):
    """App com `TakeoverStore` real em `tmp_path/config` e log em `tmp_path/logs`."""
    monkeypatch.setattr(
        evolution_mod,
        "settings",
        SimpleNamespace(log_dir=tmp_path / "logs", evolution_instance="minha-instancia"),
    )
    takeover = TakeoverStore(tmp_path / "config")
    for cid in assumidas:
        takeover.assumir(cid)
    sender, chamadas = _sender_mudo()
    conversation = FakeConversation()
    return build_app(conversation, sender, takeover=takeover, config=ConfigFake()), conversation, chamadas


@pytest.mark.asyncio
async def test_webhook_em_modo_humano_nao_chama_o_agente_e_loga_inbound(tmp_path, monkeypatch):
    app, conversation, chamadas = _com_takeover(tmp_path, monkeypatch, ["wa-5511999999999"])

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/webhook", json=_payload(texto="oi, tem alguém aí?"))

    assert resp.status_code == 200
    assert conversation.recebidos == []        # o agente não respondeu
    assert chamadas == []                      # nem mandou nada pelo WhatsApp

    arquivo = tmp_path / "logs" / f"{nome_arquivo_log('wa-5511999999999')}.jsonl"
    assert "5511999999999" not in arquivo.name          # o telefone não vai para o disco
    linhas = arquivo.read_text(encoding="utf-8").splitlines()
    evento = json.loads(linhas[0])
    assert evento["event"] == "inbound"
    assert evento["message_id"] == "3EB0ABC"
    assert evento["data"] == {
        "text": "oi, tem alguém aí?",
        "media_type": "text",
        "sender_name": "Fulano",
        "origem": "whatsapp:minha-instancia",
        "modo": "humano",
    }


@pytest.mark.asyncio
async def test_webhook_com_takeover_de_outro_lead_segue_normal(tmp_path, monkeypatch):
    app, conversation, chamadas = _com_takeover(tmp_path, monkeypatch, ["wa-5511000000000"])

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/webhook", json=_payload(texto="quero cotar"))

    assert [i.text for i in conversation.recebidos] == ["quero cotar"]
    assert [c["path"] for c in chamadas if c["path"].startswith("/message/sendText/")]
    assert not (tmp_path / "logs").exists()    # quem loga o turno é a Conversation (aqui, falsa)


@pytest.mark.asyncio
async def test_devolver_volta_a_chamar_o_agente(tmp_path, monkeypatch):
    app, conversation, _ = _com_takeover(tmp_path, monkeypatch, ["wa-5511999999999"])
    takeover = TakeoverStore(tmp_path / "config")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/webhook", json=_payload(texto="oi", message_id="m1"))
        takeover.devolver("wa-5511999999999")          # operador devolve, sem reiniciar o processo
        await client.post("/webhook", json=_payload(texto="e aí", message_id="m2"))

    assert [i.text for i in conversation.recebidos] == ["e aí"]
