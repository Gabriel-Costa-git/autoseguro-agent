"""Testes da orquestração do turno (sem rede, sem LLM, sem docker).

`policy`/`presenter`/`quote_client`/`cep`/logger entram como fakes: aqui se testa o
que o `conversation.py` faz com as ações, não a decisão em si.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import httpx
import pytest

from agent import cep as cep_mod
from agent.conversation import TEXTO_ERRO, TEXTO_LENTO, Conversation, InMemoryStateStore
from agent.models import (
    AnswerWithTools,
    AskField,
    AskPlan,
    CepInfo,
    ConfirmCep,
    DoQuotes,
    Extraction,
    Handoff,
    HandoffReason,
    Inbound,
    Intent,
    LeadState,
    Outbound,
    Present,
    PresentMany,
    Refuse,
    SendText,
    Stage,
    VeiculoColetado,
)
from agent.observability import ConversationLogger
from agent.policy import next_action as next_action_real
from agent.presenter import render as render_real
from agent.quote_client import QuoteClient
from agent.rules import Rules
from agent.runtime_config import ConfigStore
from tests.fakes import (
    FakeCepLookup,
    FakeExtractor,
    FakeLogger,
    FakeQuoteClient,
    FakeResponder,
    FakeRules,
    ScriptedPolicy,
    logger_factory_unico,
    quote_indisponivel,
    quote_ok,
    quote_recusa,
    quote_request,
    render_fake,
)

HOJE = date(2026, 9, 1)


def montar(
    tmp_path: Path,
    passos,
    extracoes=None,
    erro_extractor=None,
    resultados=None,
    lento=False,
    cep_info=None,
    lookup=None,
    on_handoff=None,
):
    logger = FakeLogger(tmp_path, "c-teste")
    policy = ScriptedPolicy(passos)
    extractor = FakeExtractor(extracoes, erro=erro_extractor)
    responder = FakeResponder()
    quote_client = FakeQuoteClient(resultados or [], lento=lento)
    lookup = lookup or FakeCepLookup(cep_info or CepInfo(cep="01310100", existe=True, cidade="São Paulo", uf="SP"))
    conv = Conversation(
        rules=FakeRules(),
        quote_client=quote_client,
        extractor=extractor,
        responder=responder,
        log_dir=tmp_path,
        store=InMemoryStateStore(),
        today=lambda: HOJE,
        next_action=policy,
        render=render_fake,
        lookup_cep=lookup,
        logger_factory=logger_factory_unico(logger),
        on_handoff=on_handoff,
    )
    return conv, {
        "logger": logger,
        "policy": policy,
        "extractor": extractor,
        "responder": responder,
        "quote_client": quote_client,
        "lookup": lookup,
    }


async def falar(conv: Conversation, texto: str | None, n: int = 1, media: str = "text") -> tuple[LeadState, list[Outbound]]:
    saidas: list[Outbound] = []

    async def emit(out: Outbound) -> None:
        saidas.append(out)

    inbound = Inbound(conversation_id="c-teste", message_id=f"m{n}", text=texto, media_type=media)  # type: ignore[arg-type]
    state = await conv.handle(inbound, emit)
    return state, saidas


# --------------------------------------------------------------------------- caminho feliz
def passos_felizes():
    """Roteiro da policy (ordem F8): idade → plano → veículo → cep (+lookup) → cotação."""

    def p1(s, e):
        s.idade = 35
        s.stage = Stage.ESCOLHA_PLANO
        return s, [AskPlan(planos=FakeRules().planos_resumo())]

    def p2(s, e):
        s.plano_id = "completo"
        s.stage = Stage.COLETA_VEICULO
        return s, [AskField(campo="veiculo")]

    def p3(s, e):
        s.veiculos = [VeiculoColetado(texto="Onix", ano=2019)]
        s.veiculo_texto, s.veiculo_ano = "Onix", 2019
        s.stage = Stage.COLETA_CEP
        return s, [AskField(campo="cep")]

    def p4(s, e):
        s.cep = "01310100"
        s.stage = Stage.CONFIRMA_CEP
        return s, []

    def p4b(s, e):
        assert s.cep_info is not None
        return s, [ConfirmCep(cep=s.cep, cidade=s.cep_info.cidade, uf=s.cep_info.uf)]

    def p5(s, e):
        s.cep_confirmado = True
        s.stage = Stage.COTANDO
        return s, [DoQuotes(requests=[quote_request()])]

    def p5b(s, e):
        s.stage = Stage.APRESENTADO
        return s, [Present(result=s.veiculos[0].quote_result or s.quote_result)]

    return [p1, p2, p3, p4, p4b, p5, p5b]


@pytest.mark.asyncio
async def test_caminho_feliz_ate_present(tmp_path):
    conv, deps = montar(
        tmp_path,
        passos_felizes(),
        extracoes=[
            Extraction(intent=Intent.FORNECER_DADOS, idade=35),
            Extraction(intent=Intent.ESCOLHER_PLANO, plano_id="completo"),
            Extraction(intent=Intent.FORNECER_DADOS, veiculo_texto="Onix", veiculo_ano=2019),
            Extraction(intent=Intent.FORNECER_DADOS, cep="01310-100"),
            Extraction(intent=Intent.CONFIRMAR),
        ],
        resultados=[quote_ok()],
    )
    _, s1 = await falar(conv, "oi, tenho 35 anos", 1)
    _, s2 = await falar(conv, "quero o completo", 2)
    _, s3 = await falar(conv, "Onix 2019", 3)
    _, s4 = await falar(conv, "01310-100", 4)
    state, s5 = await falar(conv, "sim", 5)

    assert [o.source for o in s1] == ["template"] and "Essencial" in s1[0].text
    assert s2[0].source == "llm"                            # pergunta do carro, pelo Responder
    assert s3[0].source == "llm"                            # pergunta do CEP
    assert deps["lookup"].chamadas == ["01310100"]          # o CEP foi conferido no ViaCEP
    assert "São Paulo/SP" in s4[0].text and s4[0].source == "template"
    assert "R$ 209.90" in s5[0].text                        # preço só na renderização da Quote
    assert state.stage is Stage.APRESENTADO
    assert deps["quote_client"].pedidos[0].plano_id == "completo"


@pytest.mark.asyncio
async def test_estado_persiste_entre_turnos(tmp_path):
    conv, _ = montar(
        tmp_path,
        passos_felizes()[:3],
        extracoes=[Extraction(idade=35), Extraction(plano_id="completo"), Extraction(veiculo_ano=2019)],
    )
    await falar(conv, "35", 1)
    await falar(conv, "o completo", 2)
    state, _ = await falar(conv, "Onix 2019", 3)
    assert state.idade == 35 and state.plano_id == "completo" and state.veiculo_ano == 2019


@pytest.mark.asyncio
async def test_ask_field_registra_ultima_pergunta_e_passa_diretiva_ao_llm(tmp_path):
    conv, deps = montar(tmp_path, passos_felizes()[1:2], extracoes=[Extraction(plano_id="completo")])
    state, _ = await falar(conv, "o completo", 1)
    assert state.ultima_pergunta == "veiculo"
    assert "ano de fabricação" in deps["responder"].chamadas[0][0]


# --------------------------------------------------------------------------- cotação
@pytest.mark.asyncio
async def test_api_fora_do_ar_vira_handoff_sem_nenhum_preco(tmp_path):
    def p1(s, e):
        s.stage = Stage.COTANDO
        return s, [DoQuotes(requests=[quote_request()])]

    def p2(s, e):
        assert s.quote_result is not None and s.quote_result.outcome.value == "indisponivel"
        s.stage = Stage.HANDOFF
        s.handoff_reason = HandoffReason.COTACAO_INDISPONIVEL
        return s, [Handoff(reason=HandoffReason.COTACAO_INDISPONIVEL, payload={"dados": {"idade": 35}})]

    conv, deps = montar(
        tmp_path,
        [p1, p2],
        extracoes=[Extraction(intent=Intent.ESCOLHER_PLANO, plano_id="completo")],
        resultados=[quote_indisponivel(4)],
    )
    state, saidas = await falar(conv, "quero o completo", 1)

    assert state.stage is Stage.HANDOFF
    assert state.handoff_reason is HandoffReason.COTACAO_INDISPONIVEL
    assert all("R$" not in o.text for o in saidas)
    eventos = deps["logger"].eventos()
    assert len([e for e in eventos if e["event"] == "quote_attempt"]) == 4
    handoff = next(e for e in eventos if e["event"] == "handoff")
    assert handoff["data"]["payload"] == {"dados": {"idade": 35}}


@pytest.mark.asyncio
async def test_cotacao_lenta_avisa_o_lead_antes_do_resultado(tmp_path):
    def p1(s, e):
        return s, [DoQuotes(requests=[quote_request()])]

    def p2(s, e):
        s.stage = Stage.APRESENTADO
        return s, [Present(result=s.quote_result)]

    conv, _ = montar(tmp_path, [p1, p2], extracoes=[Extraction()], resultados=[quote_ok()], lento=True)
    _, saidas = await falar(conv, "completo", 1)
    assert saidas[0].text == TEXTO_LENTO
    assert "R$" in saidas[1].text


@pytest.mark.asyncio
async def test_recusa_da_api_e_registrada_como_refusal(tmp_path):
    def p1(s, e):
        return s, [DoQuotes(requests=[quote_request(idade=80)])]

    def p2(s, e):
        s.stage = Stage.ENCERRADO_RECUSA
        return s, [Refuse(motivo=s.quote_result.motivo_recusa)]

    conv, deps = montar(tmp_path, [p1, p2], extracoes=[Extraction()], resultados=[quote_recusa()])
    state, saidas = await falar(conv, "sou o condutor", 1)

    assert state.stage is Stage.ENCERRADO_RECUSA
    assert "idade fora da faixa aceita" in saidas[0].text
    assert any(e["event"] == "refusal" for e in deps["logger"].eventos())


# --------------------------------------------------------------------------- bordas
@pytest.mark.asyncio
async def test_midia_sem_texto_nao_chama_o_llm(tmp_path):
    def p1(s, e):
        assert e is None
        return s, [SendText(text="Não consigo ouvir áudio por aqui. Pode me escrever?")]

    conv, deps = montar(tmp_path, [p1])
    _, saidas = await falar(conv, None, 1, media="audio")
    assert deps["extractor"].chamadas == []
    assert deps["policy"].chamadas == [None]
    assert saidas[0].source == "template"


@pytest.mark.asyncio
async def test_texto_vazio_e_tratado_como_midia(tmp_path):
    def p1(s, e):
        return s, [SendText(text="Pode escrever de novo?")]

    conv, deps = montar(tmp_path, [p1])
    await falar(conv, "   ", 1)
    assert deps["extractor"].chamadas == []


@pytest.mark.asyncio
async def test_falha_inesperada_responde_neutro_e_escala(tmp_path):
    conv, deps = montar(tmp_path, [], erro_extractor=RuntimeError("cep 01310-100 quebrou o parser"))
    state, saidas = await falar(conv, "oi", 1)

    assert saidas[0].text == TEXTO_ERRO
    assert state.stage is Stage.HANDOFF
    assert state.handoff_reason is HandoffReason.ERRO_INTERNO
    eventos = deps["logger"].eventos()
    erro = next(e for e in eventos if e["event"] == "error")
    assert erro["data"]["erro"] == "RuntimeError"
    assert "Traceback" not in str(eventos)


@pytest.mark.asyncio
async def test_acao_desconhecida_nao_deixa_o_lead_sem_resposta(tmp_path):
    """Ação não prevista é bug nosso: vira handoff, nunca silêncio."""

    def p1(s, e):
        return s, [DoQuotes(requests=[quote_request()])]

    conv, _ = montar(tmp_path, [p1], extracoes=[Extraction()], resultados=[])
    state, saidas = await falar(conv, "oi", 1)   # FakeQuoteClient sem resultado → IndexError
    assert saidas[0].text == TEXTO_ERRO
    assert state.stage is Stage.HANDOFF


# --------------------------------------------------------------------------- log
@pytest.mark.asyncio
async def test_log_jsonl_tem_a_trilha_completa_do_turno(tmp_path):
    def p1(s, e):
        s.stage = Stage.COTANDO
        return s, [DoQuotes(requests=[quote_request()])]

    def p2(s, e):
        s.stage = Stage.APRESENTADO
        return s, [Present(result=s.quote_result)]

    conv, deps = montar(
        tmp_path,
        [p1, p2],
        extracoes=[Extraction(intent=Intent.ESCOLHER_PLANO, plano_id="completo")],
        resultados=[quote_indisponivel(2)],
    )
    await falar(conv, "quero o completo", 7)

    eventos = deps["logger"].eventos()
    kinds = [e["event"] for e in eventos]
    for esperado in ("inbound", "extraction", "decision", "quote_attempt", "quote_result", "outbound"):
        assert esperado in kinds, kinds
    assert kinds.count("quote_attempt") == 2
    assert all(e["message_id"] is not None for e in eventos)
    assert all(e["quote_id"] == "q-down" for e in eventos if e["event"].startswith("quote_"))
    assert next(e for e in eventos if e["event"] == "extraction")["data"]["plano_id"] == "completo"
    decisao = next(e for e in eventos if e["event"] == "decision")
    assert decisao["data"]["actions"] == ["do_quotes"] and decisao["data"]["stage"] == "cotando"
    assert next(e for e in eventos if e["event"] == "llm_call")["data"]["papel"] == "extractor"


# --------------------------------------------------------------------------- origem
@pytest.mark.asyncio
async def test_origem_do_canal_entra_no_state_e_no_log(tmp_path):
    """O canal diz de onde veio o lead; o turno guarda no estado e no evento `inbound`."""
    conv, deps = montar(tmp_path, [lambda s, e: (s, [])], extracoes=[Extraction()])

    async def emit(_out):
        return None

    await conv.handle(
        Inbound(conversation_id="c-teste", message_id="m1", text="oi", origem="whatsapp:corretora", sender_name="Ana"),
        emit,
    )

    assert conv.store.get("c-teste").origem == "whatsapp:corretora"
    evento = next(e for e in deps["logger"].eventos() if e["event"] == "inbound")
    assert evento["data"]["origem"] == "whatsapp:corretora"
    assert evento["data"]["sender_name"] == "Ana"


@pytest.mark.asyncio
async def test_origem_e_a_do_primeiro_contato(tmp_path):
    """Mensagem seguinte sem origem (canal antigo, replay) não apaga a que já está no estado."""
    conv, _ = montar(tmp_path, [lambda s, e: (s, []), lambda s, e: (s, [])], extracoes=[Extraction(), Extraction()])

    async def emit(_out):
        return None

    await conv.handle(Inbound(conversation_id="c-teste", message_id="m1", text="oi", origem="cli"), emit)
    await conv.handle(Inbound(conversation_id="c-teste", message_id="m2", text="35"), emit)

    assert conv.store.get("c-teste").origem == "cli"


@pytest.mark.asyncio
async def test_sem_origem_o_campo_fica_none(tmp_path):
    """Comportamento entregue: canal que não informa origem continua funcionando igual."""
    conv, deps = montar(tmp_path, [lambda s, e: (s, [])], extracoes=[Extraction()])
    state, _ = await falar(conv, "oi", 1)
    assert state.origem is None
    assert next(e for e in deps["logger"].eventos() if e["event"] == "inbound")["data"]["origem"] is None


# --------------------------------------------------------------------------- integração real
# Aqui só o LLM é dublê: policy, presenter, rules, QuoteClient, cep e logger são os reais,
# com httpx.MockTransport no lugar da rede. É o teste que prova que as peças se encaixam.
PLANOS = {
    "moeda": "BRL",
    "planos": [
        {"id": "essencial", "nome": "Essencial", "base_mensal": 119.90, "franquia": 4500,
         "coberturas": ["colisao", "roubo", "furto"]},
        {"id": "completo", "nome": "Completo", "base_mensal": 209.90, "franquia": 3000,
         "coberturas": ["colisao", "roubo", "furto", "terceiros", "vidros"]},
        {"id": "premium", "nome": "Premium", "base_mensal": 339.90, "franquia": 1500,
         "coberturas": ["colisao", "roubo", "furto", "terceiros", "vidros", "carro_reserva"]},
    ],
    "regras": {
        "faixa_etaria": [
            {"idade_min": 18, "idade_max": 24, "multiplicador": 1.60},
            {"idade_min": 25, "idade_max": 29, "multiplicador": 1.25},
            {"idade_min": 30, "idade_max": 59, "multiplicador": 1.00},
            {"idade_min": 60, "idade_max": 75, "multiplicador": 1.40},
            {"idade_min": 76, "idade_max": 200, "recusar": True, "motivo": "Idade acima do limite."},
        ],
        "idade_veiculo": [
            {"anos_min": 0, "anos_max": 5, "multiplicador": 1.00},
            {"anos_min": 6, "anos_max": 10, "multiplicador": 1.15},
            {"anos_min": 11, "anos_max": 20, "multiplicador": 1.45},
            {"anos_min": 21, "anos_max": 200, "recusar": True, "motivo": "Veículo muito antigo."},
        ],
        "regiao_cep": {"prefixos_alto_risco": ["07", "08", "21", "26", "59"], "multiplicador": 1.30},
        "carencia": {"coberturas_com_carencia": ["roubo", "furto"], "dias": 30},
    },
}

QUOTE_200 = {
    "plano_id": "completo", "plano_nome": "Completo", "premio_mensal": 209.9, "franquia": 3000,
    "coberturas": ["colisao", "roubo", "furto", "terceiros", "vidros"],
    "multiplicadores": {"faixa_etaria": 1.0, "idade_veiculo": 1.15, "regiao": 1.0},
    "carencia": {"coberturas": ["roubo", "furto"], "dias": 30, "observacao": "Carência de 30 dias."},
    "moeda": "BRL",
}


def _conversa_real(tmp_path: Path, respostas_quote: list[int], extracoes=None):
    """Monta a conversa com os módulos reais; `respostas_quote` são os status do POST /quote.

    `extracoes` é o roteiro do Extractor. Ele muda de tamanho conforme o texto do lead: o
    pré-parser do `conversation` resolve "01310-100" e "sim" sem LLM, e nesses turnos NENHUMA
    entrada do roteiro é consumida — é exatamente a economia que ele existe para fazer.
    """
    restantes = list(respostas_quote)

    def quote_handler(request: httpx.Request) -> httpx.Response:
        status = restantes.pop(0) if restantes else 200
        if status != 200:
            return httpx.Response(status, json={"error": "upstream_unavailable"})
        # A API do desafio devolve o plano que foi pedido; o fixture é de um plano só, então
        # aqui o id/nome são ecoados — senão o lead escolhe "essencial" e recebe "Completo".
        plano_id = __import__("json").loads(request.content or b"{}").get("plano_id", "completo")
        corpo = {**QUOTE_200, "plano_id": plano_id, "plano_nome": plano_id.capitalize()}
        return httpx.Response(200, json=corpo)

    def viacep_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"localidade": "São Paulo", "uf": "SP"})

    async def sem_sono(_: float) -> None:
        return None

    async def lookup(cep8: str, timeout_s: float = 2.0):
        async with httpx.AsyncClient(transport=httpx.MockTransport(viacep_handler)) as client:
            return await cep_mod.lookup_cep(cep8, timeout_s, client=client)

    conv = Conversation(
        rules=Rules.from_planos(PLANOS, HOJE),
        quote_client=QuoteClient("http://api", transport=httpx.MockTransport(quote_handler), sleep=sem_sono),
        extractor=FakeExtractor(
            extracoes
            if extracoes is not None
            else [
                Extraction(intent=Intent.FORNECER_DADOS, idade=35),
                Extraction(intent=Intent.ESCOLHER_PLANO, plano_id="completo"),
                Extraction(intent=Intent.FORNECER_DADOS, veiculo_texto="Onix", veiculo_ano=2019),
                Extraction(intent=Intent.FORNECER_DADOS, cep="01310-100"),
                Extraction(intent=Intent.CONFIRMAR),
                Extraction(intent=Intent.FORNECER_DADOS, data_inicio=HOJE),
            ]
        ),
        responder=FakeResponder(),
        log_dir=tmp_path,
        store=InMemoryStateStore(),
        today=lambda: HOJE,
        next_action=next_action_real,
        render=render_real,
        lookup_cep=lookup,
        logger_factory=ConversationLogger,
    )
    return conv


@pytest.mark.asyncio
async def test_integracao_caminho_feliz_com_policy_presenter_e_cliente_reais(tmp_path):
    conv = _conversa_real(tmp_path, [503, 200])   # a API cai uma vez e o retry resolve
    _, s1 = await falar(conv, "oi, tenho 35 anos", 1)
    _, s2 = await falar(conv, "quero o completo", 2)
    _, s3 = await falar(conv, "é um Onix 2019", 3)
    _, s4 = await falar(conv, "meu cep é 01310-100", 4)
    _, s5 = await falar(conv, "sim, é isso", 5)
    state, s6 = await falar(conv, "pode ser hoje mesmo", 6)

    assert "Essencial" in s1[0].text and "209,90" not in s1[0].text   # planos sem prêmio, só franquia
    # Pergunta seca fora do 1º turno é o texto do slot: o Responder não é chamado (template-first).
    assert s2[0].source == "template" and "ano" in s2[0].text.lower()
    assert s3[0].source == "template"
    assert "São Paulo" in s4[0].text                     # ViaCEP real (mockado no transporte)
    # A data é o último campo da coleta: sem ela, o pro-rata apareceria sem ninguém ter falado nisso.
    assert s5[0].source == "template" and "hoje" in s5[0].text.lower()
    assert state.stage is Stage.APRESENTADO
    assert "R$ 209,90" in s6[-1].text                    # único preço da conversa, vindo da API

    eventos = [
        __import__("json").loads(linha)
        for linha in (tmp_path / "c-teste.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    kinds = [e["event"] for e in eventos]
    assert kinds.count("quote_attempt") == 2 and "quote_result" in kinds
    assert "01310-***" in str(eventos) and "01310100" not in str(eventos)   # PII mascarada pelo logger


@pytest.mark.asyncio
async def test_integracao_api_fora_do_ar_escala_sem_inventar_preco(tmp_path):
    # "01310-100" e "sim" são resolvidos pelo pré-parser: o roteiro do Extractor só tem os
    # quatro turnos que realmente vão ao LLM.
    conv = _conversa_real(
        tmp_path,
        [503, 502, 500, 503],
        extracoes=[
            Extraction(intent=Intent.FORNECER_DADOS, idade=35),
            Extraction(intent=Intent.ESCOLHER_PLANO, plano_id="completo"),
            Extraction(intent=Intent.FORNECER_DADOS, veiculo_texto="Onix", veiculo_ano=2019),
            Extraction(intent=Intent.FORNECER_DADOS, data_inicio=HOJE),
        ],
    )
    todas: list[Outbound] = []
    roteiro = ["tenho 35", "o completo", "Onix 2019", "01310-100", "sim", "hoje mesmo"]
    for n, texto in enumerate(roteiro, start=1):
        state, saidas = await falar(conv, texto, n)
        todas.extend(saidas)

    assert len(conv.extractor.chamadas) == 4        # 6 turnos, 4 chamadas: o pré-parser pegou 2
    assert state.stage is Stage.HANDOFF
    assert state.handoff_reason is HandoffReason.COTACAO_INDISPONIVEL
    # Nenhuma mensagem carrega mensalidade: sem cotação, valor nenhum sai (franquia dos
    # planos vem do /planos e não é preço do lead).
    assert all("209,90" not in o.text and "/mês" not in o.text for o in todas)
    eventos = (tmp_path / "c-teste.jsonl").read_text(encoding="utf-8")
    assert '"quote_attempt"' in eventos and eventos.count('"quote_attempt"') == 4


# --------------------------------------------------------------------------- Studio (config editável)
def _config(tmp_path: Path, monkeypatch, **overrides) -> ConfigStore:
    """Store isolado (nunca toca `config/`) no lugar do singleton do conversation."""
    loja = ConfigStore(tmp_path / "cfg")
    if overrides:
        loja.set_overrides("tools", overrides)
    monkeypatch.setattr("agent.conversation.config_store", loja)
    return loja


def _passos_cep():
    def p1(s, e):
        s.cep = "01310100"
        s.stage = Stage.CONFIRMA_CEP
        return s, []

    def p2(s, e):
        s.cep_confirmado = True
        s.stage = Stage.ESCOLHA_PLANO
        return s, [SendText(text="beleza")]

    return [p1, p2]


@pytest.mark.asyncio
async def test_viacep_desligado_nao_consulta_e_aceita_o_cep(tmp_path, monkeypatch):
    """Toggle do Studio: sem rede, o CEP entra como não confirmado (`existe=None`)."""
    _config(tmp_path, monkeypatch, viacep={"enabled": False})
    conv, deps = montar(tmp_path, _passos_cep(), extracoes=[Extraction(cep="01310-100")])
    state, saidas = await falar(conv, "01310-100", 1)

    assert deps["lookup"].chamadas == []
    assert state.cep_info == CepInfo(cep="01310100", existe=None)
    assert saidas[0].text == "beleza"
    lookup_ev = next(e for e in deps["logger"].eventos() if e["event"] == "cep_lookup")
    assert lookup_ev["data"]["skipped"] is True
    assert lookup_ev["data"]["existe"] is None


@pytest.mark.asyncio
async def test_viacep_ligado_continua_consultando(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch)
    conv, deps = montar(tmp_path, _passos_cep(), extracoes=[Extraction(cep="01310-100")])
    state, _ = await falar(conv, "01310-100", 1)

    assert deps["lookup"].chamadas == ["01310100"]
    assert state.cep_info.existe is True
    lookup_ev = next(e for e in deps["logger"].eventos() if e["event"] == "cep_lookup")
    assert "skipped" not in lookup_ev["data"]


@pytest.mark.asyncio
async def test_timeout_do_viacep_vem_do_store_quando_nao_injetado(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, viacep={"timeout_s": 0.25})
    vistos: list[float] = []

    async def lookup(cep8: str, timeout_s: float = 2.0) -> CepInfo:
        vistos.append(timeout_s)
        return CepInfo(cep=cep8, existe=True, cidade="São Paulo", uf="SP")

    conv, _ = montar(tmp_path, _passos_cep(), extracoes=[Extraction(cep="01310-100")], lookup=lookup)
    await falar(conv, "01310-100", 1)
    assert vistos == [0.25]


@pytest.mark.asyncio
async def test_texto_lento_vem_do_slot_ativo(tmp_path, monkeypatch):
    loja = _config(tmp_path, monkeypatch)
    loja.add_version("conversation.texto_lento", "v2", "Aguenta aí que estou consultando.")

    def p1(s, e):
        return s, [DoQuotes(requests=[quote_request()])]

    def p2(s, e):
        s.stage = Stage.APRESENTADO
        return s, [Present(result=s.quote_result)]

    conv, _ = montar(tmp_path, [p1, p2], extracoes=[Extraction()], resultados=[quote_ok()], lento=True)
    _, saidas = await falar(conv, "completo", 1)
    assert saidas[0].text == "Aguenta aí que estou consultando."


@pytest.mark.asyncio
async def test_texto_de_erro_vem_do_slot_ativo(tmp_path, monkeypatch):
    loja = _config(tmp_path, monkeypatch)
    loja.add_version("conversation.texto_erro", "v2", "Deu ruim aqui; já chamei alguém.")

    conv, _ = montar(tmp_path, [], erro_extractor=RuntimeError("boom"))
    state, saidas = await falar(conv, "oi", 1)
    assert saidas[0].text == "Deu ruim aqui; já chamei alguém."
    assert state.stage is Stage.HANDOFF


# --------------------------------------------------------------------------- consulta com ferramenta
TOOL_APOLICE = {
    "tipo": "http",
    "descricao": "Consulta a apólice do cliente pelo CPF.",
    "parametros": {"cpf": {"tipo": "string", "obrigatorio": True}},
    "http": {"metodo": "GET", "url": "https://api.exemplo.test/apolices/{cpf}"},
}


def _com_tool(tmp_path, monkeypatch, ligada: bool = True) -> ConfigStore:
    """Store isolado no lugar do singleton do conversation, com (ou sem) uma tool habilitada."""
    loja = _config(tmp_path, monkeypatch)
    if ligada:
        loja.upsert_custom_tool("consulta_apolice", TOOL_APOLICE)
    return loja


@pytest.mark.asyncio
async def test_consulta_sem_tool_habilitada_vira_outro(tmp_path, monkeypatch):
    """O valor está no schema do `Extraction`: o modelo pode escolhê-lo mesmo sem ferramenta."""
    _com_tool(tmp_path, monkeypatch, ligada=False)
    conv, deps = montar(
        tmp_path, [lambda s, e: (s, [])], extracoes=[Extraction(intent=Intent.CONSULTA, idade=35)]
    )
    await falar(conv, "minha apólice está ativa?", 1)

    assert deps["policy"].chamadas[0].intent is Intent.OUTRO       # a policy nem vê `consulta`
    evento = next(e for e in deps["logger"].eventos() if e["event"] == "extraction")
    assert evento["data"]["intent"] == "outro"
    assert evento["data"]["intent_bruto"] == "consulta"            # o sinal do modelo não some
    assert evento["data"]["idade"] == 35                           # o resto da extração fica igual


@pytest.mark.asyncio
async def test_consulta_com_tool_habilitada_chega_na_policy(tmp_path, monkeypatch):
    _com_tool(tmp_path, monkeypatch)
    conv, deps = montar(
        tmp_path, [lambda s, e: (s, [])], extracoes=[Extraction(intent=Intent.CONSULTA, idade=35)]
    )
    await falar(conv, "minha apólice está ativa?", 1)

    assert deps["policy"].chamadas[0].intent is Intent.CONSULTA
    evento = next(e for e in deps["logger"].eventos() if e["event"] == "extraction")
    assert evento["data"]["intent"] == "consulta"
    assert "intent_bruto" not in evento["data"]


@pytest.mark.asyncio
async def test_answer_with_tools_passa_pelo_responder_e_entra_no_log(tmp_path, monkeypatch):
    _com_tool(tmp_path, monkeypatch)

    def p1(s, e):
        return s, [AnswerWithTools(directive="use a ferramenta e retome: pergunte a idade")]

    conv, deps = montar(tmp_path, [p1], extracoes=[Extraction(intent=Intent.CONSULTA)])
    _, saidas = await falar(conv, "minha apólice está ativa?", 1)

    assert deps["responder"].chamadas == [("use a ferramenta e retome: pergunte a idade", "minha apólice está ativa?")]
    assert saidas[0].source == "llm"
    decisao = next(e for e in deps["logger"].eventos() if e["event"] == "decision")
    assert decisao["data"]["actions"] == ["answer_with_tools"]


@pytest.mark.asyncio
async def test_turno_de_consulta_ponta_a_ponta_com_a_policy_real(tmp_path, monkeypatch):
    """Extração `consulta` + tool ligada ⇒ a policy real decide `answer_with_tools`."""
    _com_tool(tmp_path, monkeypatch)
    monkeypatch.setattr("agent.policy.store", ConfigStore(tmp_path / "cfg"))
    logger = FakeLogger(tmp_path, "c-teste")
    conv = Conversation(
        rules=Rules.from_planos(PLANOS, HOJE),
        quote_client=FakeQuoteClient([]),
        extractor=FakeExtractor([Extraction(intent=Intent.CONSULTA, idade=35)]),
        responder=FakeResponder("Sua apólice está ativa. Qual o ano do carro?"),
        log_dir=tmp_path,
        store=InMemoryStateStore(),
        today=lambda: HOJE,
        render=render_fake,
        logger_factory=logger_factory_unico(logger),
    )
    state, saidas = await falar(conv, "tenho 35 anos. minha apólice do cpf 12345678901 está ativa?", 1)

    assert state.idade == 35                       # o campo da mesma mensagem foi aplicado
    assert state.stage is Stage.INICIO             # a etapa não andou por causa da dúvida
    assert state.ultima_pergunta == "plano"        # mas a próxima pergunta foi feita (ordem F8)
    assert saidas[0].text == "Sua apólice está ativa. Qual o ano do carro?"
    decisao = next(e for e in logger.eventos() if e["event"] == "decision")
    assert decisao["data"]["actions"] == ["answer_with_tools"]


# --------------------------------------------------------------------------- vários carros (F8)
@pytest.mark.asyncio
async def test_dois_carros_cotam_em_paralelo_e_saem_numa_mensagem_so(tmp_path):
    """Duas chamadas à API, um resultado por carro no estado, UMA mensagem para o lead."""

    def p1(s, e):
        s.veiculos = [
            VeiculoColetado(texto="Onix 2022", ano=2022),
            VeiculoColetado(texto="HB20 2020", ano=2020),
        ]
        s.stage = Stage.COTANDO
        return s, [DoQuotes(requests=[quote_request(veiculo_ano=2022), quote_request(veiculo_ano=2020)])]

    def p2(s, e):
        s.stage = Stage.APRESENTADO
        return s, [PresentMany(resultados=s.veiculos)]

    conv, deps = montar(
        tmp_path, [p1, p2], extracoes=[Extraction()], resultados=[quote_ok(), quote_ok(idade=40)]
    )
    state, saidas = await falar(conv, "quero cotar os dois", 1)

    assert [r.veiculo_ano for r in deps["quote_client"].pedidos] == [2022, 2020]
    assert [v.quote_result.request.idade for v in state.veiculos] == [35, 40]   # cada carro com o seu
    assert state.quote_result is state.veiculos[0].quote_result                 # espelho do primeiro
    assert len(saidas) == 1                                                     # uma mensagem só

    eventos = deps["logger"].eventos()
    assert [e["event"] for e in eventos].count("quote_result") == 2
    assert [e["data"]["veiculo"] for e in eventos if e["event"] == "quote_result"] == [
        "Onix 2022", "HB20 2020",
    ]


@pytest.mark.asyncio
async def test_aviso_de_cotacao_lenta_sai_uma_vez_so_com_dois_carros(tmp_path):
    def p1(s, e):
        s.veiculos = [VeiculoColetado(texto="Onix", ano=2022), VeiculoColetado(texto="HB20", ano=2020)]
        s.stage = Stage.COTANDO
        return s, [DoQuotes(requests=[quote_request(), quote_request()])]

    def p2(s, e):
        s.stage = Stage.APRESENTADO
        return s, [PresentMany(resultados=s.veiculos)]

    conv, _ = montar(
        tmp_path, [p1, p2], extracoes=[Extraction()],
        resultados=[quote_ok(), quote_ok()], lento=True,
    )
    _, saidas = await falar(conv, "vamos lá", 1)

    lentos = [o for o in saidas if o.text == TEXTO_LENTO]
    assert len(lentos) == 1


# --------------------------------------------------------------------------- aviso de handoff (F9)
def _passo_handoff(payload=None):
    def passo(s, e):
        s.stage = Stage.HANDOFF
        s.handoff_reason = HandoffReason.LEAD_PEDIU_HUMANO
        return s, [Handoff(reason=HandoffReason.LEAD_PEDIU_HUMANO, payload=payload or {"dados": {}})]

    return passo


@pytest.mark.asyncio
async def test_on_handoff_e_chamado_uma_vez_e_so_depois_do_lead_ser_avisado(tmp_path):
    ordem: list[str] = []
    avisos: list[tuple] = []

    async def on_handoff(state, action):
        ordem.append("aviso")
        avisos.append((state.conversation_id, action.reason))

    conv, _ = montar(
        tmp_path, [_passo_handoff()], extracoes=[Extraction(intent=Intent.PEDIR_HUMANO)],
        on_handoff=on_handoff,
    )
    saidas: list[Outbound] = []

    async def emit(out):
        ordem.append("outbound")
        saidas.append(out)

    await conv.handle(Inbound(conversation_id="c-teste", message_id="m1", text="quero um humano"), emit)

    assert ordem == ["outbound", "aviso"]      # o lead vem primeiro; o consultor depois
    assert avisos == [("c-teste", HandoffReason.LEAD_PEDIU_HUMANO)]
    assert len(saidas) == 1


@pytest.mark.asyncio
async def test_sem_gancho_o_turno_e_o_de_sempre(tmp_path):
    conv, deps = montar(tmp_path, [_passo_handoff()], extracoes=[Extraction(intent=Intent.PEDIR_HUMANO)])
    state, saidas = await falar(conv, "quero um humano", 1)

    assert state.stage is Stage.HANDOFF and len(saidas) == 1
    kinds = [e["event"] for e in deps["logger"].eventos()]
    assert "handoff" in kinds and "handoff_notice" not in kinds


@pytest.mark.asyncio
async def test_aviso_que_falha_nao_derruba_o_turno(tmp_path):
    async def on_handoff(state, action):
        raise RuntimeError("consultor inalcançável")

    conv, deps = montar(
        tmp_path, [_passo_handoff()], extracoes=[Extraction(intent=Intent.PEDIR_HUMANO)],
        on_handoff=on_handoff,
    )
    state, saidas = await falar(conv, "quero um humano", 1)

    assert state.stage is Stage.HANDOFF          # o turno terminou normalmente
    assert len(saidas) == 1
    evento = next(e for e in deps["logger"].eventos() if e["event"] == "handoff_notice")
    assert evento["data"] == {"canal": "notifier", "status": "erro", "erro": "RuntimeError: consultor inalcançável"}


@pytest.mark.asyncio
async def test_erro_interno_tambem_avisa_o_consultor(tmp_path):
    """Turno quebrado vira handoff: sem o aviso, ninguém do outro lado ficaria sabendo."""
    avisos: list[tuple] = []

    async def on_handoff(state, action):
        avisos.append((action.reason, action.payload.get("motivo")))

    conv, _ = montar(tmp_path, [], erro_extractor=RuntimeError("boom"), on_handoff=on_handoff)
    state, saidas = await falar(conv, "oi", 1)

    assert state.stage is Stage.HANDOFF
    assert saidas[0].text == TEXTO_ERRO
    assert avisos == [(HandoffReason.ERRO_INTERNO, "erro_interno")]



# --------------------------------------------------------------------------- goldens de fluxo
# Cinco roteiros inteiros, byte a byte: o que o LEAD lê, na ordem em que lê, e o estado em que a
# conversa parou. Os testes acima provam pedaços; estes travam a conversa completa — é o que
# pega a regressão que nenhuma asserção isolada vê (uma pergunta a mais, uma frase que sumiu,
# uma etapa que trocou de lugar). Regravar é decisão explícita:
#
#     ATUALIZAR_GOLDENS=1 uv run pytest tests/test_conversation.py -k fluxo
#
# e o diff dos `.txt` tem de ser lido no code review como se fosse código.
GOLDEN_FLUXO_DIR = Path(__file__).parent / "golden"


def _roteiro_feliz():
    return (
        [
            ("oi, tenho 35 anos", None),
            ("quero o completo", None),
            ("é um Onix 2019", None),
            ("meu cep é 01310-100", None),
            ("sim, é isso", None),
            ("pode ser hoje", None),
        ],
        [
            Extraction(intent=Intent.FORNECER_DADOS, idade=35),
            Extraction(intent=Intent.ESCOLHER_PLANO, plano_id="completo"),
            Extraction(intent=Intent.FORNECER_DADOS, veiculo_texto="Onix", veiculo_ano=2019),
            Extraction(intent=Intent.FORNECER_DADOS, cep="01310-100"),
            Extraction(intent=Intent.CONFIRMAR),
            Extraction(intent=Intent.FORNECER_DADOS, data_inicio=HOJE),
        ],
        [200],
    )


def _roteiro_falha_api():
    return (
        [
            ("tenho 35 anos", None),
            ("o completo", None),
            ("Onix 2019", None),
            ("01310-100", None),          # o pré-parser resolve: não consome extração
            ("sim", None),                # idem
            ("hoje mesmo", None),
        ],
        [
            Extraction(intent=Intent.FORNECER_DADOS, idade=35),
            Extraction(intent=Intent.ESCOLHER_PLANO, plano_id="completo"),
            Extraction(intent=Intent.FORNECER_DADOS, veiculo_texto="Onix", veiculo_ano=2019),
            Extraction(intent=Intent.FORNECER_DADOS, data_inicio=HOJE),
        ],
        [503, 502, 500, 503],
    )


def _roteiro_recusa():
    return (
        [("tenho 90 anos", None), ("que pena", None)],
        [
            Extraction(intent=Intent.FORNECER_DADOS, idade=90),
            Extraction(intent=Intent.OUTRO),
        ],
        [200],
    )


def _roteiro_cep_invalido():
    return (
        [
            ("tenho 35 anos", None),
            ("o essencial", None),
            ("Onix 2019", None),
            ("123", None),                # pré-parser: dígitos que não formam CEP → vai ao LLM
            ("abc", None),
            ("hoje", None),
        ],
        [
            Extraction(intent=Intent.FORNECER_DADOS, idade=35),
            Extraction(intent=Intent.ESCOLHER_PLANO, plano_id="essencial"),
            Extraction(intent=Intent.FORNECER_DADOS, veiculo_texto="Onix", veiculo_ano=2019),
            Extraction(intent=Intent.FORNECER_DADOS, cep="123"),
            Extraction(intent=Intent.FORNECER_DADOS, cep="abc"),
            Extraction(intent=Intent.FORNECER_DADOS, data_inicio=HOJE),
        ],
        [200],
    )


def _roteiro_reabertura():
    return (
        [
            ("tenho 35 anos", None),
            ("o completo", None),
            ("é um Corsa 2001", None),
            ("então cota pro meu outro carro, um Onix 2019", None),
        ],
        [
            Extraction(intent=Intent.FORNECER_DADOS, idade=35),
            Extraction(intent=Intent.ESCOLHER_PLANO, plano_id="completo"),
            Extraction(intent=Intent.FORNECER_DADOS, veiculo_texto="Corsa", veiculo_ano=2001),
            Extraction(intent=Intent.FORNECER_DADOS, veiculo_texto="Onix", veiculo_ano=2019),
        ],
        [200],
    )


ROTEIROS = {
    "feliz": _roteiro_feliz,
    "falha_api": _roteiro_falha_api,
    "recusa": _roteiro_recusa,
    "cep_invalido": _roteiro_cep_invalido,
    "reabertura": _roteiro_reabertura,
}


async def _transcrever(tmp_path: Path, nome: str) -> str:
    """Roda o roteiro na conversa REAL e devolve a transcrição determinística."""
    mensagens, extracoes, quotes = ROTEIROS[nome]()
    conv = _conversa_real(tmp_path, quotes, extracoes=extracoes)
    linhas = [f"# fluxo_{nome}"]
    state = None
    for n, (texto, media) in enumerate(mensagens, start=1):
        state, saidas = await falar(conv, texto, n, media=media or "text")
        linhas.append(f"lead: {texto}")
        if not saidas:
            linhas.append("  (sem resposta)")
        for out in saidas:
            for i, linha in enumerate((out.text or "").split("\n")):
                prefixo = f"  agente [{out.source}]: " if i == 0 else "    "
                linhas.append(f"{prefixo}{linha}".rstrip())
    assert state is not None
    linhas.append("")
    linhas.append(
        "estado: stage={} handoff={} cep_ausente={} data_assumida={} chamadas_extractor={}".format(
            state.stage.value,
            state.handoff_reason.value if state.handoff_reason else "-",
            state.cep_ausente,
            state.data_assumida,
            len(conv.extractor.chamadas),
        )
    )
    return "\n".join(linhas) + "\n"


@pytest.mark.parametrize("nome", sorted(ROTEIROS))
@pytest.mark.asyncio
async def test_fluxo_igual_ao_golden(tmp_path, nome: str):
    transcricao = await _transcrever(tmp_path, nome)
    alvo = GOLDEN_FLUXO_DIR / f"fluxo_{nome}.txt"
    if os.environ.get("ATUALIZAR_GOLDENS"):
        alvo.write_text(transcricao, encoding="utf-8")
    assert transcricao == alvo.read_text(encoding="utf-8")
