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
from agent.channels.evolution import EvolutionSender
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
    """Registra o que iria para o WhatsApp. `erro` levanta (Evolution fora do ar) e
    `entregue=False` recusa (número inválido, instância desconectada)."""

    def __init__(self, erro: Exception | None = None, entregue: bool | None = True) -> None:
        self.enviadas: list[tuple[str, str]] = []
        self.erro = erro
        self.entregue = entregue

    async def send_text(self, number: str, text: str) -> bool | None:
        if self.erro is not None:
            raise self.erro
        self.enviadas.append((number, text))
        return self.entregue


def _com_consultor(loja) -> None:
    """Liga o canal WhatsApp: sem nenhum canal de aviso, o takeover não acontece mais."""
    loja.set_overrides("tools", {"handoff": {"consultor_number": "5511977770000"}})


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
    kw.setdefault("sleep", _sleep_falso([]))
    return HandoffNotifier(store=loja, logger_factory=logger_factory_unico(logger), **kw)


def _sleep_falso(registro: list[float]):
    """Backoff sem espera de verdade: a suíte roda sem relógio (nem rede, nem LLM)."""
    async def dormir(segundos: float) -> None:
        registro.append(segundos)
    return dormir


def _eventos(logger: FakeLogger) -> dict[str, dict]:
    """`handoff_notice` por canal — cada canal grava um."""
    return {e["data"]["canal"]: e["data"] for e in logger.eventos() if e["event"] == "handoff_notice"}


# --------------------------------------------------------------------------- 1. takeover
@pytest.mark.asyncio
async def test_takeover_assume_a_conversa_depois_de_um_aviso_entregue(loja, logger, tmp_path):
    _com_consultor(loja)
    takeover = TakeoverStore(tmp_path / "config")
    await _notificador(loja, logger, takeover=takeover, sender=FakeSender())(_state(), _acao())

    assert takeover.is_humano(CID) is True          # o webhook para de chamar o agente
    assert _eventos(logger)["takeover"] == {"canal": "takeover", "status": "ok", "destino": CID}
    assert takeover.listar()[CID]["por"] == "agente"   # foi o handoff, então pode expirar


@pytest.mark.asyncio
async def test_sem_nenhum_aviso_entregue_o_agente_continua_respondendo(loja, logger, tmp_path):
    """O pior caso da F9: o agente calava e o consultor nunca ficava sabendo."""
    _com_consultor(loja)
    takeover = TakeoverStore(tmp_path / "config")
    sender = FakeSender(erro=httpx.ConnectError("evolution fora"))
    await _notificador(loja, logger, takeover=takeover, sender=sender)(_state(), _acao())

    assert takeover.is_humano(CID) is False         # ninguém foi avisado: o agente segue
    evento = _eventos(logger)["takeover"]
    assert evento["status"] == "nao_assumido"
    assert evento["motivo"] == "whatsapp=erro, webhook=desligado"


@pytest.mark.asyncio
async def test_numero_invalido_do_consultor_nao_vira_status_ok(loja, logger, tmp_path):
    """400 da Evolution: o `EvolutionSender` devolve False em vez de engolir o erro."""
    _com_consultor(loja)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(400, json={})))
    sender = EvolutionSender("http://evolution.test", "chave", "instancia", client=client)
    takeover = TakeoverStore(tmp_path / "config")
    await _notificador(loja, logger, takeover=takeover, sender=sender)(_state(), _acao())

    evento = _eventos(logger)["whatsapp"]
    assert evento["status"] == "erro"
    assert "recusado" in evento["erro"]
    assert takeover.is_humano(CID) is False
    assert _eventos(logger)["takeover"]["status"] == "nao_assumido"


@pytest.mark.asyncio
async def test_webhook_ok_sozinho_ja_autoriza_o_takeover(loja, logger, tmp_path):
    """Um canal basta: quem recebeu o lead foi o CRM."""
    loja.set_overrides("tools", {"handoff": {"webhook_url": "https://crm.exemplo.test/leads"}})
    takeover = TakeoverStore(tmp_path / "config")
    await _notificador(loja, logger, takeover=takeover, client=_webhook_ok({}))(_state(), _acao())

    assert takeover.is_humano(CID) is True
    assert _eventos(logger)["takeover"]["status"] == "ok"


@pytest.mark.asyncio
async def test_sender_antigo_que_nao_devolve_nada_continua_valendo_como_enviado(loja, logger):
    _com_consultor(loja)
    await _notificador(loja, logger, sender=FakeSender(entregue=None))(_state(), _acao())
    assert _eventos(logger)["whatsapp"]["status"] == "ok"


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
        def assumir(self, cid, *, automatico=False):
            raise OSError("disco cheio")

    _com_consultor(loja)
    notificador = _notificador(loja, logger, takeover=TakeoverQuebrado(), sender=FakeSender())
    await notificador(_state(), _acao())
    evento = _eventos(logger)["takeover"]
    assert evento["status"] == "erro" and "OSError" in evento["erro"]


# --------------------------------------------------------------------------- 2. WhatsApp
@pytest.mark.asyncio
async def test_consultor_recebe_o_resumo_com_preco_e_link(loja, logger):
    _com_consultor(loja)
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
async def test_webhook_5xx_e_repetido_e_o_segundo_200_conta_como_ok(loja, logger):
    """CRM instável é a terceira dependência da casa: 5xx é retry, como na `/quote`."""
    loja.set_overrides("tools", {"handoff": {"webhook_url": "https://crm.exemplo.test/leads"}})
    respostas = [httpx.Response(503, text="indisponível"), httpx.Response(200, json={"ok": True})]
    dormiu: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return respostas.pop(0)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notificador = _notificador(loja, logger, client=client, sleep=_sleep_falso(dormiu))
    await notificador(_state(), _acao())

    evento = _eventos(logger)["webhook"]
    assert evento["status"] == "ok" and evento["tentativas"] == 2
    assert dormiu == [0.5]                    # backoff 0,5 s entre a 1ª e a 2ª


@pytest.mark.asyncio
async def test_webhook_5xx_sempre_esgota_tres_tentativas(loja, logger):
    loja.set_overrides("tools", {"handoff": {"webhook_url": "https://crm.exemplo.test/leads"}})
    chamadas: list[int] = []
    dormiu: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas.append(1)
        return httpx.Response(500, text="boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _notificador(loja, logger, client=client, sleep=_sleep_falso(dormiu))(_state(), _acao())

    assert len(chamadas) == 3
    assert dormiu == [0.5, 1.0]
    evento = _eventos(logger)["webhook"]
    assert evento["status"] == "erro" and evento["tentativas"] == 3


@pytest.mark.asyncio
async def test_webhook_4xx_nao_e_repetido(loja, logger):
    """403/404 é contrato errado (URL, token): insistir só repete o mesmo erro."""
    loja.set_overrides("tools", {"handoff": {"webhook_url": "https://crm.exemplo.test/leads"}})
    chamadas: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas.append(1)
        return httpx.Response(403, text="sem permissão")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _notificador(loja, logger, client=client, sleep=_sleep_falso([]))(_state(), _acao())

    assert len(chamadas) == 1
    assert _eventos(logger)["webhook"]["erro"] == "HTTP 403"


@pytest.mark.asyncio
async def test_todos_os_canais_falham_lead_continua_com_o_agente(loja, logger, tmp_path):
    """Aceite do brief: WhatsApp fora + CRM fora = ninguém avisado = takeover não acontece."""
    loja.set_overrides("tools", {"handoff": {
        "consultor_number": "5511977770000", "webhook_url": "https://crm.exemplo.test/leads",
    }})
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(500, text="boom")))
    takeover = TakeoverStore(tmp_path / "config")
    notificador = _notificador(
        loja, logger, takeover=takeover, sender=FakeSender(entregue=False),
        client=client, sleep=_sleep_falso([]),
    )
    await notificador(_state(), _acao())

    eventos = _eventos(logger)
    assert eventos["whatsapp"]["status"] == "erro"
    assert eventos["webhook"]["status"] == "erro"
    assert eventos["takeover"]["status"] == "nao_assumido"
    assert eventos["takeover"]["motivo"] == "whatsapp=erro, webhook=erro"
    assert takeover.is_humano(CID) is False


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
async def test_os_tres_canais_gravam_um_evento_cada_e_o_takeover_e_o_ultimo(loja, logger, tmp_path):
    """A ordem é a decisão: avisar primeiro, passar a bola só depois de alguém ter pegado."""
    await _notificador(loja, logger, takeover=TakeoverStore(tmp_path / "config"))(_state(), _acao())
    canais = [e["data"]["canal"] for e in logger.eventos() if e["event"] == "handoff_notice"]
    assert canais == ["whatsapp", "webhook", "takeover"]
