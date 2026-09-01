"""Testes da orquestração do turno (sem rede, sem LLM, sem docker).

`policy`/`presenter`/`quote_client`/`cep`/logger entram como fakes: aqui se testa o
que o `conversation.py` faz com as ações, não a decisão em si.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from agent import cep as cep_mod
from agent.conversation import TEXTO_ERRO, TEXTO_LENTO, Conversation, InMemoryStateStore
from agent.models import (
    AskField,
    AskPlan,
    CepInfo,
    ConfirmCep,
    DoQuote,
    Extraction,
    Handoff,
    HandoffReason,
    Inbound,
    Intent,
    LeadState,
    Outbound,
    Present,
    Refuse,
    SendText,
    Stage,
)
from agent.observability import ConversationLogger
from agent.policy import next_action as next_action_real
from agent.presenter import render as render_real
from agent.quote_client import QuoteClient
from agent.rules import Rules
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
):
    logger = FakeLogger(tmp_path, "c-teste")
    policy = ScriptedPolicy(passos)
    extractor = FakeExtractor(extracoes, erro=erro_extractor)
    responder = FakeResponder()
    quote_client = FakeQuoteClient(resultados or [], lento=lento)
    lookup = FakeCepLookup(cep_info or CepInfo(cep="01310100", existe=True, cidade="São Paulo", uf="SP"))
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
    """Roteiro da policy: idade → veículo → cep (+lookup) → plano → cotação → apresentação."""

    def p1(s, e):
        s.idade = 35
        s.stage = Stage.COLETA_VEICULO
        return s, [AskField(campo="veiculo")]

    def p2(s, e):
        s.veiculo_texto, s.veiculo_ano = "Onix", 2019
        s.stage = Stage.COLETA_CEP
        return s, [AskField(campo="cep")]

    def p3(s, e):
        s.cep = "01310100"
        s.stage = Stage.CONFIRMA_CEP
        return s, []

    def p3b(s, e):
        assert s.cep_info is not None
        return s, [ConfirmCep(cep=s.cep, cidade=s.cep_info.cidade, uf=s.cep_info.uf)]

    def p4(s, e):
        s.cep_confirmado = True
        s.stage = Stage.ESCOLHA_PLANO
        return s, [AskPlan(planos=FakeRules().planos_resumo())]

    def p5(s, e):
        s.plano_id = "completo"
        s.stage = Stage.COTANDO
        return s, [DoQuote(request=quote_request())]

    def p5b(s, e):
        s.stage = Stage.APRESENTADO
        return s, [Present(result=s.quote_result)]

    return [p1, p2, p3, p3b, p4, p5, p5b]


@pytest.mark.asyncio
async def test_caminho_feliz_ate_present(tmp_path):
    conv, deps = montar(
        tmp_path,
        passos_felizes(),
        extracoes=[
            Extraction(intent=Intent.FORNECER_DADOS, idade=35),
            Extraction(intent=Intent.FORNECER_DADOS, veiculo_texto="Onix", veiculo_ano=2019),
            Extraction(intent=Intent.FORNECER_DADOS, cep="01310-100"),
            Extraction(intent=Intent.CONFIRMAR),
            Extraction(intent=Intent.ESCOLHER_PLANO, plano_id="completo"),
        ],
        resultados=[quote_ok()],
    )
    _, s1 = await falar(conv, "oi, tenho 35 anos", 1)
    _, s2 = await falar(conv, "Onix 2019", 2)
    _, s3 = await falar(conv, "01310-100", 3)
    _, s4 = await falar(conv, "sim", 4)
    state, s5 = await falar(conv, "quero o completo", 5)

    assert [o.source for o in s1] == ["llm"] and "ano de fabricação" in s1[0].text
    assert s2[0].source == "llm"
    assert deps["lookup"].chamadas == ["01310100"]          # o CEP foi conferido no ViaCEP
    assert "São Paulo/SP" in s3[0].text and s3[0].source == "template"
    assert "Essencial" in s4[0].text
    assert "R$ 209.90" in s5[0].text                        # preço só na renderização da Quote
    assert state.stage is Stage.APRESENTADO
    assert deps["quote_client"].pedidos[0].plano_id == "completo"


@pytest.mark.asyncio
async def test_estado_persiste_entre_turnos(tmp_path):
    conv, _ = montar(
        tmp_path,
        passos_felizes()[:2],
        extracoes=[Extraction(idade=35), Extraction(veiculo_ano=2019)],
    )
    await falar(conv, "35", 1)
    state, _ = await falar(conv, "Onix 2019", 2)
    assert state.idade == 35 and state.veiculo_ano == 2019


@pytest.mark.asyncio
async def test_ask_field_registra_ultima_pergunta_e_passa_diretiva_ao_llm(tmp_path):
    conv, deps = montar(tmp_path, passos_felizes()[:1], extracoes=[Extraction(idade=35)])
    state, _ = await falar(conv, "tenho 35", 1)
    assert state.ultima_pergunta == "veiculo"
    assert "ano de fabricação" in deps["responder"].chamadas[0][0]


# --------------------------------------------------------------------------- cotação
@pytest.mark.asyncio
async def test_api_fora_do_ar_vira_handoff_sem_nenhum_preco(tmp_path):
    def p1(s, e):
        s.stage = Stage.COTANDO
        return s, [DoQuote(request=quote_request())]

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
        return s, [DoQuote(request=quote_request())]

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
        return s, [DoQuote(request=quote_request(idade=80))]

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
        return s, [DoQuote(request=quote_request())]

    conv, _ = montar(tmp_path, [p1], extracoes=[Extraction()], resultados=[])
    state, saidas = await falar(conv, "oi", 1)   # FakeQuoteClient sem resultado → IndexError
    assert saidas[0].text == TEXTO_ERRO
    assert state.stage is Stage.HANDOFF


# --------------------------------------------------------------------------- log
@pytest.mark.asyncio
async def test_log_jsonl_tem_a_trilha_completa_do_turno(tmp_path):
    def p1(s, e):
        s.stage = Stage.COTANDO
        return s, [DoQuote(request=quote_request())]

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
    assert decisao["data"]["actions"] == ["do_quote"] and decisao["data"]["stage"] == "cotando"
    assert next(e for e in eventos if e["event"] == "llm_call")["data"]["papel"] == "extractor"


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


def _conversa_real(tmp_path: Path, respostas_quote: list[int]):
    """Monta a conversa com os módulos reais; `respostas_quote` são os status do POST /quote."""
    restantes = list(respostas_quote)

    def quote_handler(request: httpx.Request) -> httpx.Response:
        status = restantes.pop(0) if restantes else 200
        if status == 200:
            return httpx.Response(200, json=QUOTE_200)
        return httpx.Response(status, json={"error": "upstream_unavailable"})

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
            [
                Extraction(intent=Intent.FORNECER_DADOS, idade=35),
                Extraction(intent=Intent.FORNECER_DADOS, veiculo_texto="Onix", veiculo_ano=2019),
                Extraction(intent=Intent.FORNECER_DADOS, cep="01310-100"),
                Extraction(intent=Intent.CONFIRMAR),
                Extraction(intent=Intent.ESCOLHER_PLANO, plano_id="completo"),
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
    _, s2 = await falar(conv, "é um Onix 2019", 2)
    _, s3 = await falar(conv, "meu cep é 01310-100", 3)
    _, s4 = await falar(conv, "sim, é isso", 4)
    state, s5 = await falar(conv, "quero o completo", 5)

    assert "ano" in s1[0].text.lower()
    assert s2[0].source == "llm"
    assert "São Paulo" in s3[0].text                     # ViaCEP real (mockado no transporte)
    assert "Essencial" in s4[0].text and "209,90" not in s4[0].text   # planos sem prêmio, só franquia
    assert state.stage is Stage.APRESENTADO
    assert "R$ 209,90" in s5[-1].text                    # único preço da conversa, vindo da API

    eventos = [
        __import__("json").loads(linha)
        for linha in (tmp_path / "c-teste.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    kinds = [e["event"] for e in eventos]
    assert kinds.count("quote_attempt") == 2 and "quote_result" in kinds
    assert "01310-***" in str(eventos) and "01310100" not in str(eventos)   # PII mascarada pelo logger


@pytest.mark.asyncio
async def test_integracao_api_fora_do_ar_escala_sem_inventar_preco(tmp_path):
    conv = _conversa_real(tmp_path, [503, 502, 500, 503])
    todas: list[Outbound] = []
    for n, texto in enumerate(["tenho 35", "Onix 2019", "01310-100", "sim", "o completo"], start=1):
        state, saidas = await falar(conv, texto, n)
        todas.extend(saidas)

    assert state.stage is Stage.HANDOFF
    assert state.handoff_reason is HandoffReason.COTACAO_INDISPONIVEL
    # Nenhuma mensagem carrega mensalidade: sem cotação, valor nenhum sai (franquia dos
    # planos vem do /planos e não é preço do lead).
    assert all("209,90" not in o.text and "/mês" not in o.text for o in todas)
    eventos = (tmp_path / "c-teste.jsonl").read_text(encoding="utf-8")
    assert '"quote_attempt"' in eventos and eventos.count('"quote_attempt"') == 4
