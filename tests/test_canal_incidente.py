"""Regressão do incidente de 02/09 (F11): 23 respostas iguais em 80 s para uma pessoa real.

O que aconteceu: um contato mandou "Boa noite", o agente respondeu e a Evolution passou a
mandar um `messages.upsert` de protocolo (recibo) a cada ~3 s logo depois de cada envio
nosso — sem `fromMe`, sem `pushName`, sem `conversation`. O `parse_webhook` transformava
cada um em turno; o estado terminal respondia o mesmo template a todos.

Os `message_id` abaixo são os do incidente (`log de produção (não versionado)`);
o telefone real foi trocado por `5511999990000`. Nada aqui toca a rede: transporte mockado
no sender, `ASGITransport` no app e um dublê de `Conversation`.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from agent.channels.evolution import build_app
from agent.models import Inbound, LeadState, Outbound
from tests.fakes import FakeLogger
from tests.test_evolution import ConfigFake

NUMERO = "5511999990000"
CID = f"wa-{NUMERO}"

# A primeira mensagem — a única que uma pessoa escreveu.
ID_BOA_NOITE = "3EB02931369ACEEF1C8093"

# Os 23 upserts que vieram depois, um por resposta nossa.
IDS_PROTOCOLO = [
    "CEC83682030A37B29796", "CEF0C031F6C74D96641D", "CE2945F5DC6293A74171",
    "CEACF02C900DE1E82638", "CE01D1F5385363293162", "CE81D6F5C805747D37AC",
    "CEAAB8F81806EA193626", "3EB09383B214A3D278D297", "CE54D1478304F2874233",
    "CED75C46D4875295FA4F", "3EB0812DDCC812F413C771", "CEEE02FF5B661ED3442F",
    "3EB03338CB4B124DDF07F4", "CE2A799E20FFE52A3D71", "3EB0F41F3924581CEAAB3A",
    "CEAB7E24209F5B88FC67", "CEB6DF0867AF4EFE89E5", "CE3A7754D64202209AE8",
    "CE33A2AECC5C4665277E", "CE1D546062AC2EFB49E9", "CEFABA4ACCBBC00CDA9B",
    "CE72F76F7A84D0CE5988", "CE77261736316ED1E813",
]

# O que a Evolution manda dentro de `message` nesses eventos, na proporção observada:
# a maioria vem vazia ou com chave de sessão; os `3EB0*` traziam `protocolMessage`.
MENSAGENS_DE_PROTOCOLO: list[dict[str, Any]] = [
    {},
    {"senderKeyDistributionMessage": {"groupId": "status@broadcast"}},
    {"messageContextInfo": {"deviceListMetadataVersion": 2}},
    {"protocolMessage": {"type": "REVOKE"}},
]

TEXTO_TERMINAL = "Um consultor já está com o seu caso e responde por aqui mesmo. Pode deixar a mensagem que ele vê."


def _upsert(message_id: str, message: dict[str, Any], push_name: str | None = None) -> dict[str, Any]:
    return {
        "event": "messages.upsert",
        "instance": "referencia",
        "data": {
            "key": {"remoteJid": f"{NUMERO}@s.whatsapp.net", "fromMe": False, "id": message_id},
            "message": message,
            "messageTimestamp": 1788400496,
            "pushName": push_name,
        },
    }


def _upserts_do_incidente() -> list[dict[str, Any]]:
    return [
        _upsert(mid, MENSAGENS_DE_PROTOCOLO[i % len(MENSAGENS_DE_PROTOCOLO)])
        for i, mid in enumerate(IDS_PROTOCOLO)
    ]


class ConversaTerminal:
    """Conversa em estado terminal: responde o MESMO texto a tudo, como no incidente."""

    def __init__(self, texto: str = TEXTO_TERMINAL) -> None:
        self.texto = texto
        self.recebidos: list[Inbound] = []

    async def handle(self, inbound: Inbound, emit) -> LeadState:
        self.recebidos.append(inbound)
        await emit(
            Outbound(
                conversation_id=inbound.conversation_id,
                message_id=f"{inbound.message_id}-o1",
                text=self.texto,
                in_reply_to=inbound.message_id,
                source="template",
            )
        )
        return LeadState(conversation_id=inbound.conversation_id)


def _sender_mudo() -> tuple[Any, list[dict[str, Any]]]:
    from agent.channels.evolution import EvolutionSender

    chamadas: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas.append({"path": request.url.path, "body": json.loads(request.content)})
        return httpx.Response(200, json={"status": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return EvolutionSender("http://evolution.test", "chave", "referencia", client=client), chamadas


def _app(tmp_path, conversation, **config):
    sender, chamadas = _sender_mudo()
    app = build_app(
        conversation,
        sender,
        config=ConfigFake(**config),
        logger_factory=lambda cid: FakeLogger(tmp_path, cid),
    )
    return app, chamadas


def _enviados(chamadas: list[dict[str, Any]]) -> list[str]:
    return [c["body"]["text"] for c in chamadas if c["path"].startswith("/message/sendText/")]


def _eventos(tmp_path, cid: str = CID) -> list[dict[str, Any]]:
    return FakeLogger(tmp_path, cid).eventos()


# --------------------------------------------------------------------------- o incidente
@pytest.mark.asyncio
async def test_os_23_upserts_de_protocolo_nao_viram_nem_um_turno(tmp_path):
    """Aceite do brief: 23 upserts de protocolo → zero `outbound`."""
    conversation = ConversaTerminal()
    app, chamadas = _app(tmp_path, conversation)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        respostas = [await client.post("/webhook", json=p) for p in _upserts_do_incidente()]

    assert len(respostas) == 23
    assert all(r.json() == {"ignored": True} for r in respostas)
    assert conversation.recebidos == []          # nenhum virou turno
    assert _enviados(chamadas) == []             # nenhuma mensagem para a pessoa
    assert _eventos(tmp_path) == []              # nada a suprimir: não chegou a nascer


@pytest.mark.asyncio
async def test_a_conversa_do_incidente_inteira_gera_uma_resposta_so(tmp_path):
    """"Boa noite" + os 23 recibos: uma pessoa falou uma vez, recebe uma resposta."""
    conversation = ConversaTerminal()
    app, chamadas = _app(tmp_path, conversation)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/webhook", json=_upsert(ID_BOA_NOITE, {"conversation": "Boa noite"}, "Ana Souza"))
        for payload in _upserts_do_incidente():
            await client.post("/webhook", json=payload)

    assert [i.text for i in conversation.recebidos] == ["Boa noite"]
    assert _enviados(chamadas) == [TEXTO_TERMINAL]


@pytest.mark.asyncio
async def test_midia_de_verdade_no_meio_do_protocolo_continua_sendo_atendida(tmp_path):
    """O filtro não pode calar o lead: áudio é mensagem, recibo não é."""
    conversation = ConversaTerminal()
    app, chamadas = _app(tmp_path, conversation)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/webhook", json=_upsert("m-recibo", {"protocolMessage": {}}))
        await client.post("/webhook", json=_upsert("m-audio", {"audioMessage": {"seconds": 3}}))

    assert [i.media_type for i in conversation.recebidos] == ["audio"]
    assert _enviados(chamadas) == [TEXTO_TERMINAL]


# --------------------------------------------------------------------------- aceite: 10 em 10 s
@pytest.mark.asyncio
async def test_dez_mensagens_seguidas_no_estado_terminal_viram_um_turno_so(tmp_path):
    """Aceite do brief, com o debounce ligado: rajada do mesmo lead = um turno, uma resposta.

    Os webhooks vão em paralelo porque é assim que chegam — a Evolution não espera a
    resposta de um para mandar o próximo. O tempo é encurtado (0,2 s no lugar de 2 s)
    para a suíte seguir rodando em segundos; o que o teste mede é a regra, não o número.
    """
    conversation = ConversaTerminal()
    app, chamadas = _app(tmp_path, conversation, debounce_s=0.2)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        tarefas = []
        for n in range(10):
            tarefas.append(
                asyncio.create_task(client.post("/webhook", json=_upsert(f"m{n}", {"conversation": f"mensagem {n}"})))
            )
            await asyncio.sleep(0.01)          # 10 mensagens numa janela menor que o debounce
        await asyncio.gather(*tarefas)

    assert len(conversation.recebidos) == 1
    assert conversation.recebidos[0].text == "\n".join(f"mensagem {n}" for n in range(10))
    assert _enviados(chamadas) == [TEXTO_TERMINAL]


@pytest.mark.asyncio
async def test_dez_mensagens_espacadas_no_estado_terminal_param_no_teto(tmp_path):
    """Sem debounce (mensagens espaçadas), quem segura é o teto por minuto.

    Com `max_respostas_por_minuto=1` fica o aceite literal: uma resposta sai, as outras
    nove viram `outbound_suprimido` com motivo `rate_limit` — e ficam no log, porque
    silêncio sem rastro é pior que ruído.
    """
    conversation = ConversaTerminal()
    app, chamadas = _app(tmp_path, conversation, debounce_s=0.0, max_respostas_por_minuto=1)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for n in range(10):
            await client.post("/webhook", json=_upsert(f"m{n}", {"conversation": f"mensagem {n}"}))

    assert len(conversation.recebidos) == 10          # o agente processou todas
    assert _enviados(chamadas) == [TEXTO_TERMINAL]    # mas só uma chegou na pessoa

    suprimidos = [e for e in _eventos(tmp_path) if e["event"] == "outbound_suprimido"]
    assert len(suprimidos) == 9
    assert {e["data"]["motivo"] for e in suprimidos} == {"rate_limit"}
    assert suprimidos[0]["data"]["limite"] == 1
    assert suprimidos[0]["data"]["in_reply_to"] == "m1"
