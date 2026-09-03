"""Catálogo fresco: `GET /planos` com TTL, cópia velha em vez de conversa derrubada, e as
`Rules` recalculadas com o `today()` de AGORA.

Duas coisas estragam regra derivada e nenhuma delas aparece em teste de unidade comum: um
`/planos` que muda com o processo no ar (a auditoria viu o agente oferecendo um plano que a
API já não aceitava) e um processo que atravessa a virada do ano com o `today` do boot
congelado. Os testes aqui são justamente sobre o tempo passando.
"""
from __future__ import annotations

import copy
import logging
from datetime import date
from pathlib import Path

import httpx
import pytest

from agent.channels.cli import _provedor_de_regras
from agent.conversation import Conversation, InMemoryStateStore
from agent.models import (
    CepInfo,
    Extraction,
    Intent,
    LeadState,
    QuoteRequest,
    Stage,
    VeiculoColetado,
)
from agent.policy import next_action as next_action_real
from agent.presenter import render as render_real
from agent.quote_client import QuoteClient, QuotePlanosUnavailable
from agent.rules import Rules
from tests.fakes import (
    PLANOS,
    QUOTE_200,
    FakeCepLookup,
    FakeExtractor,
    FakeLogger,
    FakeResponder,
    logger_factory_unico,
    sem_sono,
)

HOJE = date(2026, 9, 1)


class Relogio:
    """Relógio monotônico que só anda quando o teste manda (`avancar`)."""

    def __init__(self) -> None:
        self.agora = 1000.0

    def __call__(self) -> float:
        return self.agora

    def avancar(self, segundos: float) -> None:
        self.agora += segundos


class ApiDePlanos:
    """`MockTransport` com catálogo TROCÁVEL: é assim que "o /planos mudou" vira teste."""

    def __init__(self, planos: dict | None = None, status: list[int] | None = None) -> None:
        self.planos = copy.deepcopy(planos or PLANOS)
        self.status = list(status or [])
        self.gets = 0
        self.quotes = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handler)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/planos":
            self.gets += 1
            status = self.status.pop(0) if self.status else 200
            if status != 200:
                return httpx.Response(status, json={"error": "upstream_unavailable"})
            return httpx.Response(200, json=self.planos)
        self.quotes += 1
        return httpx.Response(200, json=QUOTE_200)


def _client(api: ApiDePlanos, relogio: Relogio, **kw) -> QuoteClient:
    return QuoteClient(
        "http://api.local", transport=api.transport(), sleep=sem_sono, clock=relogio, **kw
    )


# --------------------------------------------------------------------------- TTL
@pytest.mark.asyncio
async def test_dentro_do_ttl_nao_refaz_o_get():
    api, relogio = ApiDePlanos(), Relogio()
    client = _client(api, relogio, planos_ttl_s=90.0)

    primeiro = await client.planos()
    relogio.avancar(89.0)
    segundo = await client.planos()

    assert api.gets == 1
    assert primeiro.origem == "http" and segundo.origem == "cache"
    assert segundo.idade_s == pytest.approx(89.0)
    assert segundo.planos is primeiro.planos      # mesmo objeto: reler é de graça


@pytest.mark.asyncio
async def test_fora_do_ttl_refaz_o_get_e_ve_o_catalogo_novo():
    api, relogio = ApiDePlanos(), Relogio()
    client = _client(api, relogio, planos_ttl_s=90.0)
    await client.planos()

    api.planos["planos"].append(
        {"id": "essencial_plus", "nome": "Essencial Plus", "base_mensal": 149.9, "franquia": 4000,
         "coberturas": ["colisao", "roubo"]}
    )
    relogio.avancar(91.0)
    depois = await client.planos()

    assert api.gets == 2
    assert depois.origem == "http"
    assert [p["id"] for p in depois.planos["planos"]][-1] == "essencial_plus"


@pytest.mark.asyncio
async def test_ttl_zero_rele_a_cada_uso():
    api, relogio = ApiDePlanos(), Relogio()
    client = _client(api, relogio, planos_ttl_s=0.0)
    await client.planos()
    await client.planos()
    assert api.gets == 2


# --------------------------------------------------------------------------- degradação
@pytest.mark.asyncio
async def test_falha_depois_de_sucesso_devolve_a_copia_marcada_stale():
    """Errar a validade das regras por alguns minutos é MUITO menos grave que calar a conversa."""
    api, relogio = ApiDePlanos(status=[200, 503, 503, 503, 503]), Relogio()
    client = _client(api, relogio, planos_ttl_s=90.0)
    bom = await client.planos()

    relogio.avancar(120.0)
    velho = await client.planos()

    assert velho.origem == "stale" and velho.stale is True
    assert velho.planos is bom.planos
    assert velho.idade_s == pytest.approx(120.0)
    assert "503" in (velho.erro or "")
    assert client.ultimo_planos is velho          # é daqui que sai o `planos_refresh` do turno


@pytest.mark.asyncio
async def test_get_planos_usa_o_mesmo_retry_do_quote():
    api, relogio = ApiDePlanos(status=[503, 500, 200]), Relogio()
    client = _client(api, relogio)

    snapshot = await client.planos()

    assert api.gets == 3 and snapshot.origem == "http"


@pytest.mark.asyncio
async def test_4xx_no_planos_nao_e_retentado():
    """Contrato errado não melhora insistindo — a mesma regra do `quote()` com 400/422."""
    api, relogio = ApiDePlanos(status=[404]), Relogio()
    client = _client(api, relogio)

    with pytest.raises(QuotePlanosUnavailable) as exc:
        await client.planos()

    assert api.gets == 1
    assert "404" in str(exc.value)


@pytest.mark.asyncio
async def test_boot_sem_api_continua_falhando_claro():
    """Sem NENHUMA cópia, levantar é o certo: agente sem regras é agente que inventa regra."""
    api, relogio = ApiDePlanos(status=[503] * 8), Relogio()
    client = _client(api, relogio)

    with pytest.raises(QuotePlanosUnavailable) as exc:
        await client.get_planos()

    assert "503" in str(exc.value)
    assert client.ultimo_planos is None


# --------------------------------------------------------------------------- provider de regras
@pytest.mark.asyncio
async def test_provider_memoiza_por_catalogo_e_por_dia():
    api, relogio = ApiDePlanos(), Relogio()
    client = _client(api, relogio, planos_ttl_s=90.0)
    hoje = [date(2026, 12, 31)]
    provider = _provedor_de_regras(client, lambda: hoje[0])

    r1 = await provider()
    r2 = await provider()
    assert r1 is r2                               # mesmo catálogo, mesmo dia: mesmo objeto

    hoje[0] = date(2027, 1, 1)                    # o processo atravessou a virada do ano
    r3 = await provider()
    assert r3 is not r1
    assert r3.ano_max == 2027 and r1.ano_max == 2026
    assert r3.ano_min == r1.ano_min + 1           # o carro que era aceito ontem deixou de ser


@pytest.mark.asyncio
async def test_provider_ve_o_plano_novo_depois_do_ttl():
    api, relogio = ApiDePlanos(), Relogio()
    client = _client(api, relogio, planos_ttl_s=90.0)
    provider = _provedor_de_regras(client, lambda: HOJE)

    antes = await provider()
    assert "essencial_plus" not in antes.plano_ids()

    api.planos["planos"].append(
        {"id": "essencial_plus", "nome": "Essencial Plus", "base_mensal": 149.9, "franquia": 4000,
         "coberturas": ["colisao"]}
    )
    relogio.avancar(91.0)
    depois = await provider()

    assert "essencial_plus" in depois.plano_ids()


# --------------------------------------------------------------------------- catálogo malformado
def test_plano_malformado_e_filtrado_em_vez_de_derrubar_o_turno(caplog):
    planos = copy.deepcopy(PLANOS)
    planos["planos"].append({"id": "quebrado"})            # sem nome nem franquia
    planos["planos"].append({"id": "novissimo", "nome": "Novíssimo", "franquia": 900, "coberturas": []})
    rules = Rules.from_planos(planos, HOJE)

    with caplog.at_level(logging.WARNING, logger="autoseguro.rules"):
        ids = rules.plano_ids()

    assert "quebrado" not in ids
    assert ids[-1] == "novissimo"                          # id fora do Literal do Extractor: vale
    assert "quebrado" in caplog.text                       # o aviso vai para o log, não para o lead


def test_plano_id_fora_do_literal_passa_no_quote_request():
    """`QuoteRequest.plano_id` é `str`: quem manda é a lista corrente, não um Literal congelado."""
    req = QuoteRequest(plano_id="essencial_plus", idade=35, veiculo_ano=2019, data_inicio="2026-09-01")
    assert req.plano_id == "essencial_plus"


# --------------------------------------------------------------------------- no turno
def _conversa(tmp_path: Path, api: ApiDePlanos, relogio: Relogio, extracoes: list[Extraction]):
    client = _client(api, relogio, planos_ttl_s=90.0)
    logger = FakeLogger(tmp_path, "c-planos")
    conv = Conversation(
        rules=Rules.from_planos(api.planos, HOJE),
        quote_client=client,
        extractor=FakeExtractor(extracoes),
        responder=FakeResponder(),
        log_dir=tmp_path,
        store=InMemoryStateStore(),
        today=lambda: HOJE,
        next_action=next_action_real,
        render=render_real,
        lookup_cep=FakeCepLookup(CepInfo(cep="01310100", existe=True, cidade="São Paulo", uf="SP")),
        logger_factory=logger_factory_unico(logger),
        rules_provider=_provedor_de_regras(client, lambda: HOJE),
    )
    return conv, logger


@pytest.mark.asyncio
async def test_o_turno_seguinte_ja_conhece_o_plano_novo(tmp_path):
    """Catálogo relido ANTES de a mensagem ser montada: a vitrine do turno 1 não tem o plano
    novo, e a resposta do turno 2 (que também sai dos dados dos planos) já tem."""
    from tests.test_conversation import falar

    api, relogio = ApiDePlanos(), Relogio()
    conv, logger = _conversa(
        tmp_path, api, relogio,
        [
            Extraction(intent=Intent.FORNECER_DADOS, idade=35),
            Extraction(intent=Intent.DUVIDA_PRODUTO),
        ],
    )

    _, primeira = await falar(conv, "tenho 35 anos", 1)
    assert "Essencial Plus" not in primeira[0].text

    api.planos["planos"].append(
        {"id": "essencial_plus", "nome": "Essencial Plus", "base_mensal": 149.9, "franquia": 4000,
         "coberturas": ["colisao"]}
    )
    relogio.avancar(91.0)
    _, segunda = await falar(conv, "qual a franquia de cada um?", 2)

    assert "Essencial Plus" in segunda[0].text
    refresh = [e for e in logger.eventos() if e["event"] == "planos_refresh"]
    assert refresh[-1]["data"]["origem"] == "http"
    assert refresh[-1]["data"]["mudou"] is True
    assert "essencial_plus" in refresh[-1]["data"]["planos"]
    assert refresh[-1]["data"]["ano_max"] == HOJE.year


@pytest.mark.asyncio
async def test_planos_refresh_registra_o_erro_quando_a_copia_esta_velha(tmp_path):
    api, relogio = ApiDePlanos(status=[200] + [503] * 8), Relogio()
    conv, logger = _conversa(tmp_path, api, relogio, [Extraction(intent=Intent.FORNECER_DADOS, idade=35)])
    await conv.quote_client.planos()          # a cópia boa do boot

    relogio.avancar(120.0)
    from tests.test_conversation import falar

    _, saidas = await falar(conv, "tenho 35 anos", 1)

    assert saidas                              # o lead foi respondido com as regras velhas
    refresh = [e for e in logger.eventos() if e["event"] == "planos_refresh"][-1]
    assert refresh["data"]["origem"] == "stale"
    assert "503" in refresh["data"]["erro"]


@pytest.mark.asyncio
async def test_plano_que_sumiu_do_catalogo_vira_pergunta_e_nao_422(tmp_path):
    """O lead escolheu "completo" e o produto saiu de linha no meio da conversa."""
    from tests.test_conversation import falar

    api, relogio = ApiDePlanos(), Relogio()
    conv, _ = _conversa(tmp_path, api, relogio, [Extraction(intent=Intent.FORNECER_DADOS, data_inicio=HOJE)])
    conv.store.put(
        LeadState(
            conversation_id="c-teste", idade=35, plano_id="completo", cep="01310100",
            cep_confirmado=True, stage=Stage.COLETA_DATA, ultima_pergunta="data_inicio",
            data_perguntada=True,
            veiculos=[VeiculoColetado(texto="Onix 2019", ano=2019)],
        )
    )

    api.planos["planos"] = [p for p in api.planos["planos"] if p["id"] != "completo"]
    relogio.avancar(91.0)
    state, saidas = await falar(conv, "hoje", 1)

    assert api.quotes == 0                     # nenhuma cotação com um plano que não existe mais
    assert state.stage is Stage.ESCOLHA_PLANO and state.plano_id is None
    assert "Essencial" in saidas[0].text
