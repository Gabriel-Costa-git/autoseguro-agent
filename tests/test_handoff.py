"""Aviso de handoff: takeover automático, WhatsApp do consultor e webhook do CRM.

Sem rede (`httpx.MockTransport`), sem WhatsApp (sender falso) e sem tocar `config/` do repo
(`ConfigStore(tmp_path)` + `TakeoverStore(tmp_path)`).
"""
from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from agent import runtime_config
from agent.config import settings
from agent.handoff import HandoffNotifier
from agent.models import (
    Handoff,
    HandoffReason,
    LeadState,
    Stage,
    VeiculoColetado,
)
from agent.runtime_config import ConfigStore
from agent.takeover import TakeoverStore
from tests.fakes import FakeLogger, logger_factory_unico, quote_ok

CID = "wa-5511999990000"


class FakeSender:
    """Registra o que iria para o WhatsApp; opcionalmente falha, como a Evolution fora do ar."""

    def __init__(self, erro: Exception | None = None) -> None:
        self.enviadas: list[tuple[str, str]] = []
        self.erro = erro

    async def send_text(self, number: str, text: str) -> None:
        if self.erro is not None:
            raise self.erro
        self.enviadas.append((number, text))


@pytest.fixture
def loja(tmp_path, monkeypatch) -> ConfigStore:
    """Store isolado e SEM `CONSULTOR_NUMBER` do ambiente (a máquina de desenvolvimento tem um)."""
    monkeypatch.setattr(runtime_config, "settings", dataclasses.replace(settings, consultor_number=None))
    store = ConfigStore(tmp_path / "config")
    store.ensure_files()
    return store


@pytest.fixture
def logger(tmp_path) -> FakeLogger:
    return FakeLogger(tmp_path, CID)


def _state(**kw) -> LeadState:
    base = {
        "conversation_id": CID,
        "stage": Stage.HANDOFF,
        "lead_nome": "Ana Souza",
        "origem": "whatsapp:corretora",
        "idade": 35,
        "veiculos": [VeiculoColetado(texto="Onix 2022", ano=2022, quote_result=quote_ok())],
        "plano_id": "completo",
        "cep": "01310100",
    }
    base.update(kw)
    return LeadState(**base)


def _acao(reason: HandoffReason = HandoffReason.LEAD_ACEITOU, **payload) -> Handoff:
    return Handoff(reason=reason, payload=payload or {"dados": {"idade": 35}, "cotacoes": []})


def _notificador(loja, logger, **kw) -> HandoffNotifier:
    return HandoffNotifier(store=loja, logger_factory=logger_factory_unico(logger), **kw)


def _eventos(logger: FakeLogger) -> dict[str, dict]:
    """`handoff_notice` por canal — cada canal grava um."""
    return {e["data"]["canal"]: e["data"] for e in logger.eventos() if e["event"] == "handoff_notice"}


# --------------------------------------------------------------------------- 1. takeover
@pytest.mark.asyncio
async def test_takeover_assume_a_conversa_automaticamente(loja, logger, tmp_path):
    takeover = TakeoverStore(tmp_path / "config")
    await _notificador(loja, logger, takeover=takeover)(_state(), _acao())

    assert takeover.is_humano(CID) is True          # o webhook para de chamar o agente
    assert _eventos(logger)["takeover"] == {"canal": "takeover", "status": "ok", "destino": CID}


@pytest.mark.asyncio
async def test_auto_assumir_desligado_nao_marca_nada(loja, logger, tmp_path):
    loja.set_overrides("tools", {"handoff": {"auto_assumir": False}})
    takeover = TakeoverStore(tmp_path / "config")
    await _notificador(loja, logger, takeover=takeover)(_state(), _acao())

    assert takeover.is_humano(CID) is False
    assert _eventos(logger)["takeover"]["status"] == "desligado"


@pytest.mark.asyncio
async def test_falha_ao_assumir_vira_erro_sem_derrubar(loja, logger):
    class TakeoverQuebrado:
        def assumir(self, cid):
            raise OSError("disco cheio")

    await _notificador(loja, logger, takeover=TakeoverQuebrado())(_state(), _acao())
    evento = _eventos(logger)["takeover"]
    assert evento["status"] == "erro" and "OSError" in evento["erro"]


# --------------------------------------------------------------------------- 2. WhatsApp
@pytest.mark.asyncio
async def test_consultor_recebe_o_resumo_com_preco_e_link(loja, logger):
    loja.set_overrides("tools", {"handoff": {"consultor_number": "5511977770000"}})
    sender = FakeSender()
    await _notificador(loja, logger, sender=sender)(_state(), _acao())

    (numero, texto), = sender.enviadas
    assert numero == "5511977770000"
    assert "lead aceitou a cotação" in texto
    assert "Ana Souza · 5511999990000 · whatsapp:corretora" in texto
    assert "idade: 35 · carros: Onix 2022 · cep: 01310-100 · plano: completo" in texto
    assert "Onix 2022 — Completo R$ 209,90/mês" in texto          # preço pode: é o consultor
    assert f"http://127.0.0.1:8765/#atendimentos/{CID}" in texto

    evento = _eventos(logger)["whatsapp"]
    assert evento["status"] == "ok"
    assert evento["destino"] == "+55 ** *****-****"               # número do consultor mascarado


@pytest.mark.asyncio
async def test_sem_consultor_number_o_canal_fica_desligado(loja, logger):
    sender = FakeSender()
    await _notificador(loja, logger, sender=sender)(_state(), _acao())

    assert sender.enviadas == []
    assert _eventos(logger)["whatsapp"] == {"canal": "whatsapp", "status": "desligado", "destino": None}


@pytest.mark.asyncio
async def test_whatsapp_fora_do_ar_vira_erro_sem_derrubar(loja, logger):
    loja.set_overrides("tools", {"handoff": {"consultor_number": "5511977770000"}})
    sender = FakeSender(erro=httpx.ConnectError("evolution fora"))
    await _notificador(loja, logger, sender=sender)(_state(), _acao())

    evento = _eventos(logger)["whatsapp"]
    assert evento["status"] == "erro" and "ConnectError" in evento["erro"]


@pytest.mark.asyncio
async def test_cotacao_pendente_e_recusada_aparecem_no_aviso(loja, logger):
    loja.set_overrides("tools", {"handoff": {"consultor_number": "5511977770000"}})
    sender = FakeSender()
    state = _state(
        veiculos=[
            VeiculoColetado(texto="Onix 2022", ano=2022, quote_result=quote_ok()),
            VeiculoColetado(texto="Fusca 1980", ano=1980),
        ]
    )
    await _notificador(loja, logger, sender=sender)(state, _acao(HandoffReason.COTACAO_INDISPONIVEL))

    _, texto = sender.enviadas[0]
    assert "cotação indisponível" in texto
    assert "Onix 2022 — Completo R$ 209,90/mês" in texto
    assert "Fusca 1980 — sem cotação" in texto


@pytest.mark.asyncio
async def test_numero_fora_do_formato_br_tambem_sai_mascarado(loja, logger):
    """Rede de segurança: o log nunca carrega o número inteiro, casando a regex de PII ou não."""
    loja.set_overrides("tools", {"handoff": {"consultor_number": "12345678901234"}})
    await _notificador(loja, logger, sender=FakeSender())(_state(), _acao())
    assert _eventos(logger)["whatsapp"]["destino"] == "***1234"


@pytest.mark.asyncio
async def test_handoff_logo_no_comeco_nao_inventa_carro(loja, logger):
    """Lead que pede humano na primeira mensagem: o aviso sai sem dados, não com um carro vazio."""
    loja.set_overrides("tools", {"handoff": {"consultor_number": "5511977770000"}})
    sender = FakeSender()
    state = LeadState(conversation_id=CID, stage=Stage.HANDOFF, lead_nome="Ana", origem="whatsapp:corretora")
    await _notificador(loja, logger, sender=sender)(state, _acao(HandoffReason.LEAD_PEDIU_HUMANO))

    _, texto = sender.enviadas[0]
    assert "nenhum carro informado ainda" in texto
    assert "carro — sem cotação" not in texto


# --------------------------------------------------------------------------- 3. webhook
def _webhook_ok(recebido: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        recebido["url"] = str(request.url)
        recebido["headers"] = dict(request.headers)
        recebido["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_webhook_recebe_o_payload_do_handoff_com_headers_resolvidos(loja, logger, monkeypatch):
    monkeypatch.setenv("CRM_TOKEN", "segredo-do-cofre")
    loja.set_overrides("tools", {"handoff": {
        "webhook_url": "https://crm.exemplo.test/leads",
        "webhook_headers": {"Authorization": "Bearer ${env:CRM_TOKEN}"},
    }})
    recebido: dict = {}
    acao = _acao(dados={"idade": 35}, cotacoes=[{"carro": "Onix 2022", "cotacao": {"outcome": "ok"}}])
    await _notificador(loja, logger, client=_webhook_ok(recebido))(_state(), acao)

    assert recebido["url"] == "https://crm.exemplo.test/leads"
    assert recebido["headers"]["authorization"] == "Bearer segredo-do-cofre"
    corpo = recebido["body"]
    assert corpo["conversation_id"] == CID
    assert corpo["origem"] == "whatsapp:corretora"
    assert corpo["motivo"] == "lead_aceitou"
    assert corpo["dados"] == {"idade": 35}                    # dados estruturados, não o texto
    assert corpo["cotacoes"][0]["carro"] == "Onix 2022"
    assert corpo["link"].endswith(f"#atendimentos/{CID}")
    assert corpo["ts"]

    evento = _eventos(logger)["webhook"]
    assert evento["status"] == "ok"
    assert evento["destino"] == "crm.exemplo.test"            # host, nunca a URL com token


@pytest.mark.asyncio
async def test_sem_webhook_url_o_canal_fica_desligado(loja, logger):
    await _notificador(loja, logger)(_state(), _acao())
    assert _eventos(logger)["webhook"]["status"] == "desligado"


@pytest.mark.asyncio
async def test_webhook_com_erro_http_e_registrado_sem_derrubar(loja, logger):
    loja.set_overrides("tools", {"handoff": {"webhook_url": "https://crm.exemplo.test/leads"}})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _notificador(loja, logger, client=client)(_state(), _acao())

    evento = _eventos(logger)["webhook"]
    assert evento["status"] == "erro" and "HTTP 500" in evento["erro"]


@pytest.mark.asyncio
async def test_webhook_com_variavel_de_ambiente_ausente_e_erro_claro(loja, logger, monkeypatch):
    monkeypatch.delenv("CRM_TOKEN", raising=False)
    loja.set_overrides("tools", {"handoff": {
        "webhook_url": "https://crm.exemplo.test/leads",
        "webhook_headers": {"Authorization": "Bearer ${env:CRM_TOKEN}"},
    }})
    recebido: dict = {}
    await _notificador(loja, logger, client=_webhook_ok(recebido))(_state(), _acao())

    assert recebido == {}                                     # nada foi enviado
    assert "CRM_TOKEN" in _eventos(logger)["webhook"]["erro"]


# --------------------------------------------------------------------------- modo simulado (Lab)
@pytest.mark.asyncio
async def test_modo_simulado_nao_envia_nem_assume_e_mostra_o_texto(loja, logger, tmp_path):
    loja.set_overrides("tools", {"handoff": {
        "consultor_number": "5511977770000", "webhook_url": "https://crm.exemplo.test/leads",
    }})
    takeover = TakeoverStore(tmp_path / "config")
    sender = FakeSender()
    recebido: dict = {}
    notificador = _notificador(
        loja, logger, sender=sender, takeover=takeover, client=_webhook_ok(recebido), simulado=True
    )
    await notificador(_state(conversation_id="lab-abc12345"), _acao())

    assert sender.enviadas == [] and recebido == {}
    assert takeover.listar() == {}
    eventos = _eventos(logger)
    assert [e["status"] for e in eventos.values()] == ["simulado", "simulado", "simulado"]
    assert "Lead para assumir" in eventos["whatsapp"]["texto"]     # o operador lê o que iria


# --------------------------------------------------------------------------- config
@pytest.mark.asyncio
async def test_studio_url_configuravel_muda_o_link(loja, logger):
    loja.set_overrides("tools", {"handoff": {
        "consultor_number": "5511977770000", "studio_url": "https://studio.corretora.test/",
    }})
    sender = FakeSender()
    await _notificador(loja, logger, sender=sender)(_state(), _acao())
    assert f"https://studio.corretora.test/#atendimentos/{CID}" in sender.enviadas[0][1]


@pytest.mark.asyncio
async def test_os_tres_canais_gravam_um_evento_cada(loja, logger, tmp_path):
    await _notificador(loja, logger, takeover=TakeoverStore(tmp_path / "config"))(_state(), _acao())
    canais = [e["data"]["canal"] for e in logger.eventos() if e["event"] == "handoff_notice"]
    assert canais == ["takeover", "whatsapp", "webhook"]
