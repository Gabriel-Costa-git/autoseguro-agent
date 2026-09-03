"""Freios de saída do canal: teto por minuto, repetição, debounce, "digitando" e memória.

Regra de leitura: o que NÃO sai vira evento `outbound_suprimido` no log da conversa, com
o motivo. Suprimir sem registrar seria trocar um bug barulhento (23 mensagens) por um
mudo (o consultor não entende por que o lead não recebeu nada).

Sem rede e sem relógio de verdade: transporte mockado no sender, `clock` injetado no
`build_app` e um dublê de `Conversation` que responde o que o teste mandar.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from agent.channels.evolution import EvolutionSender, build_app
from agent.models import Inbound, LeadState, Outbound
from tests.fakes import FakeLogger
from tests.test_canal_incidente import CID, NUMERO, _upsert
from tests.test_evolution import ConfigFake


class Relogio:
    """Relógio monotônico controlado pelo teste (o teto por minuto depende dele)."""

    def __init__(self) -> None:
        self.agora = 1000.0

    def __call__(self) -> float:
        return self.agora

    def avancar(self, segundos: float) -> None:
        self.agora += segundos


class ConversaScript:
    """Responde `respostas[i]` no i-ésimo turno (ou sempre a mesma, se for uma só)."""

    def __init__(self, respostas: list[str], eventos: list[str] | None = None) -> None:
        self.respostas = respostas
        self.recebidos: list[Inbound] = []
        self.eventos = eventos if eventos is not None else []

    async def handle(self, inbound: Inbound, emit) -> LeadState:
        self.eventos.append(f"handle:{inbound.message_id}")
        self.recebidos.append(inbound)
        i = len(self.recebidos) - 1
        texto = self.respostas[i] if i < len(self.respostas) else self.respostas[-1]
        await emit(
            Outbound(
                conversation_id=inbound.conversation_id,
                message_id=f"{inbound.message_id}-o1",
                text=texto,
                in_reply_to=inbound.message_id,
                source="template",
            )
        )
        return LeadState(conversation_id=inbound.conversation_id)


def _montar(tmp_path, conversation, clock=None, **config):
    eventos: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        corpo = json.loads(request.content)
        rotulo = "typing" if request.url.path.startswith("/chat/sendPresence/") else f"send:{corpo['text']}"
        eventos.append(rotulo)
        return httpx.Response(200, json={"status": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sender = EvolutionSender("http://evolution.test", "chave", "referencia", client=client)
    conversation.eventos = eventos
    app = build_app(
        conversation,
        sender,
        config=ConfigFake(**config),
        clock=clock or (lambda: 0.0),
        logger_factory=lambda cid: FakeLogger(tmp_path, cid),
    )
    return app, eventos


def _cliente(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _enviados(eventos: list[str]) -> list[str]:
    return [e.removeprefix("send:") for e in eventos if e.startswith("send:")]


def _suprimidos(tmp_path) -> list[dict[str, Any]]:
    return [e for e in FakeLogger(tmp_path, CID).eventos() if e["event"] == "outbound_suprimido"]


# --------------------------------------------------------------------------- repetição
@pytest.mark.asyncio
async def test_mesmo_texto_duas_vezes_seguidas_sem_o_lead_escrever_e_suprimido(tmp_path):
    """Dois áudios seguidos: o "pode me escrever?" sai uma vez, não duas."""
    conversation = ConversaScript(["Não consigo ouvir áudio/abrir arquivos por aqui. Pode me escrever?"])
    app, eventos = _montar(tmp_path, conversation)

    async with _cliente(app) as client:
        await client.post("/webhook", json=_upsert("m1", {"audioMessage": {}}))
        await client.post("/webhook", json=_upsert("m2", {"audioMessage": {}}))

    assert len(conversation.recebidos) == 2                    # os dois viraram turno
    assert len(_enviados(eventos)) == 1                        # só um chegou na pessoa

    (suprimido,) = _suprimidos(tmp_path)
    assert suprimido["data"]["motivo"] == "repetido"
    assert suprimido["data"]["in_reply_to"] == "m2"
    assert suprimido["data"]["source"] == "template"
    assert suprimido["message_id"] == "m2-o1"


@pytest.mark.asyncio
async def test_texto_do_lead_no_meio_libera_a_mesma_resposta(tmp_path):
    """Pessoa escrevendo merece resposta, mesmo que o texto seja o mesmo de antes."""
    conversation = ConversaScript(["Um consultor já está com o seu caso."])
    app, eventos = _montar(tmp_path, conversation)

    async with _cliente(app) as client:
        await client.post("/webhook", json=_upsert("m1", {"conversation": "oi"}))
        await client.post("/webhook", json=_upsert("m2", {"conversation": "tem alguém aí?"}))

    assert len(_enviados(eventos)) == 2
    assert _suprimidos(tmp_path) == []


@pytest.mark.asyncio
async def test_respostas_diferentes_nunca_sao_suprimidas_por_repeticao(tmp_path):
    conversation = ConversaScript(["primeira", "segunda"])
    app, eventos = _montar(tmp_path, conversation)

    async with _cliente(app) as client:
        await client.post("/webhook", json=_upsert("m1", {"audioMessage": {}}))
        await client.post("/webhook", json=_upsert("m2", {"audioMessage": {}}))

    assert _enviados(eventos) == ["primeira", "segunda"]


# --------------------------------------------------------------------------- teto por minuto
@pytest.mark.asyncio
async def test_teto_de_respostas_por_minuto(tmp_path):
    """Oito mensagens diferentes com o teto padrão (6): duas ficam de fora, registradas."""
    relogio = Relogio()
    conversation = ConversaScript([f"resposta {n}" for n in range(8)])
    app, eventos = _montar(tmp_path, conversation, clock=relogio)

    async with _cliente(app) as client:
        for n in range(8):
            await client.post("/webhook", json=_upsert(f"m{n}", {"conversation": f"msg {n}"}))
            relogio.avancar(5)                     # 8 mensagens em 40 s

    assert len(_enviados(eventos)) == 6
    motivos = [e["data"]["motivo"] for e in _suprimidos(tmp_path)]
    assert motivos == ["rate_limit", "rate_limit"]
    assert _suprimidos(tmp_path)[0]["data"]["janela_s"] == 60.0


@pytest.mark.asyncio
async def test_passada_a_janela_o_canal_volta_a_responder(tmp_path):
    """O teto é uma pausa, não uma mordaça: um minuto depois a conversa segue."""
    relogio = Relogio()
    conversation = ConversaScript([f"resposta {n}" for n in range(9)])
    app, eventos = _montar(tmp_path, conversation, clock=relogio, max_respostas_por_minuto=2)

    async with _cliente(app) as client:
        for n in range(3):
            await client.post("/webhook", json=_upsert(f"m{n}", {"conversation": f"msg {n}"}))
        assert len(_enviados(eventos)) == 2         # a terceira estourou o teto

        relogio.avancar(61)
        await client.post("/webhook", json=_upsert("m9", {"conversation": "e agora?"}))

    assert len(_enviados(eventos)) == 3


# --------------------------------------------------------------------------- debounce
@pytest.mark.asyncio
async def test_mensagens_picadas_viram_um_turno_com_o_texto_concatenado(tmp_path):
    conversation = ConversaScript(["ok"])
    app, _ = _montar(tmp_path, conversation, debounce_s=0.15)

    async with _cliente(app) as client:
        tarefas = [
            asyncio.create_task(client.post("/webhook", json=_upsert("m1", {"conversation": "oi"}))),
        ]
        await asyncio.sleep(0.01)
        tarefas.append(
            asyncio.create_task(client.post("/webhook", json=_upsert("m2", {"conversation": "quero cotar"})))
        )
        await asyncio.gather(*tarefas)

    (inbound,) = conversation.recebidos
    assert inbound.text == "oi\nquero cotar"
    assert inbound.message_id == "m2"              # responde à última, que é a mais recente


@pytest.mark.asyncio
async def test_debounce_zero_desliga_a_agregacao(tmp_path):
    conversation = ConversaScript(["a", "b"])
    app, _ = _montar(tmp_path, conversation, debounce_s=0.0)

    async with _cliente(app) as client:
        await client.post("/webhook", json=_upsert("m1", {"conversation": "oi"}))
        await client.post("/webhook", json=_upsert("m2", {"conversation": "quero cotar"}))

    assert [i.text for i in conversation.recebidos] == ["oi", "quero cotar"]


@pytest.mark.asyncio
async def test_audio_e_texto_no_mesmo_lote_valem_pelo_texto(tmp_path):
    """O agente não ouve áudio; se o lote tem texto, é por ele que o turno anda."""
    conversation = ConversaScript(["ok"])
    app, _ = _montar(tmp_path, conversation, debounce_s=0.15)

    async with _cliente(app) as client:
        tarefas = [asyncio.create_task(client.post("/webhook", json=_upsert("m1", {"audioMessage": {}})))]
        await asyncio.sleep(0.01)
        tarefas.append(
            asyncio.create_task(client.post("/webhook", json=_upsert("m2", {"conversation": "é um Onix 2019"})))
        )
        await asyncio.gather(*tarefas)

    (inbound,) = conversation.recebidos
    assert inbound.media_type == "text" and inbound.text == "é um Onix 2019"


# --------------------------------------------------------------------------- typing
@pytest.mark.asyncio
async def test_typing_sai_ao_entrar_no_turno_e_antes_da_resposta(tmp_path):
    """Entre a mensagem do lead e a resposta cabem extração, policy e cotação lenta: o
    "digitando" da entrada é o que diz "estou aqui" nesse intervalo."""
    conversation = ConversaScript(["pronto"])
    app, eventos = _montar(tmp_path, conversation)

    async with _cliente(app) as client:
        await client.post("/webhook", json=_upsert("m1", {"conversation": "oi"}))

    assert eventos == ["typing", "handle:m1", "typing", "send:pronto"]


@pytest.mark.asyncio
async def test_saida_suprimida_nao_manda_nem_typing(tmp_path):
    conversation = ConversaScript(["mesma coisa"])
    app, eventos = _montar(tmp_path, conversation)

    async with _cliente(app) as client:
        await client.post("/webhook", json=_upsert("m1", {"audioMessage": {}}))
        await client.post("/webhook", json=_upsert("m2", {"audioMessage": {}}))

    # segundo turno: só o typing de entrada, nada de "digitando" para uma resposta que não vai sair
    assert eventos == ["typing", "handle:m1", "typing", "send:mesma coisa", "typing", "handle:m2"]


# --------------------------------------------------------------------------- memória do canal
@pytest.mark.asyncio
async def test_conversa_parada_sai_da_memoria_do_canal(tmp_path):
    """O `defaultdict(asyncio.Lock)` da entrega guardava um lock por telefone para sempre."""
    from agent.channels.evolution import LIMPAR_A_PARTIR_DE, TTL_CONVERSA_S

    relogio = Relogio()
    conversation = ConversaScript(["ok"])
    app, _ = _montar(tmp_path, conversation, clock=relogio)

    async with _cliente(app) as client:
        for n in range(LIMPAR_A_PARTIR_DE):
            jid = f"{int(NUMERO) + n}@s.whatsapp.net"
            payload = _upsert(f"m{n}", {"conversation": "oi"})
            payload["data"]["key"]["remoteJid"] = jid
            await client.post("/webhook", json=payload)

        assert len(app.state.conversas) == LIMPAR_A_PARTIR_DE
        relogio.avancar(TTL_CONVERSA_S + 1)
        await client.post("/webhook", json=_upsert("m-novo", {"conversation": "oi de novo"}))

    assert len(app.state.conversas) == 1           # sobrou a que acabou de falar
    assert CID in app.state.conversas


@pytest.mark.asyncio
async def test_conversa_em_voo_nunca_e_esquecida(tmp_path):
    """Limpar o lock de um turno em andamento seria deixar dois turnos correrem juntos."""
    from agent.channels.evolution import _Conversa

    relogio = Relogio()
    conversation = ConversaScript(["ok"])
    app, _ = _montar(tmp_path, conversation, clock=relogio)
    conversas = app.state.conversas

    for n in range(80):
        conversas[f"wa-5511{n:09d}"] = _Conversa(visto_em=relogio())
    em_voo = conversas["wa-5511000000001"]
    await em_voo.lock.acquire()
    try:
        relogio.avancar(10_000)
        async with _cliente(app) as client:
            await client.post("/webhook", json=_upsert("m1", {"conversation": "oi"}))
    finally:
        em_voo.lock.release()

    assert "wa-5511000000001" in conversas
    assert len(conversas) == 2                     # a travada e a que acabou de chegar


# --------------------------------------------------------------------------- config torta
@pytest.mark.asyncio
async def test_config_ilegivel_cai_nos_padroes_e_o_canal_segue(tmp_path):
    """Nenhum freio pode virar mordaça por causa de um `tools.json` quebrado."""
    class LojaQuebrada:
        def param(self, path: str):
            raise ValueError("tools.json inválido")

    conversation = ConversaScript(["resposta"])
    eventos: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        corpo = json.loads(request.content)
        if request.url.path.startswith("/message/sendText/"):
            eventos.append(corpo["text"])
        return httpx.Response(200, json={"status": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sender = EvolutionSender("http://evolution.test", "chave", "referencia", client=client)
    app = build_app(conversation, sender, config=LojaQuebrada(),
                    logger_factory=lambda cid: FakeLogger(tmp_path, cid))

    async with _cliente(app) as client_app:
        await client_app.post("/webhook", json=_upsert("m1", {"conversation": "oi"}))

    assert eventos == ["resposta"]
