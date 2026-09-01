"""Testes dos templates. Puros: nenhum preço pode nascer aqui, só vir do `Quote`."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent.models import (
    AskField,
    AskPlan,
    ConfirmCep,
    DoQuote,
    Handoff,
    HandoffReason,
    LeadState,
    PlanoResumo,
    Present,
    ProRata,
    Quote,
    QuoteOutcome,
    QuoteRequest,
    QuoteResult,
    Refuse,
    Reply,
    SendText,
)
from agent.presenter import render

ESTADO = LeadState(conversation_id="conv_1")
FONTE_PRESENTER = Path(__file__).resolve().parents[1] / "agent" / "presenter.py"


def _quote(**kw) -> Quote:
    dados = {
        "plano_id": "completo",
        "plano_nome": "Completo",
        "premio_mensal": 209.9,
        "franquia": 3000,
        "coberturas": ["colisao", "roubo", "furto", "terceiros", "vidros"],
        "multiplicadores": {"faixa_etaria": 1.0},
        "carencia_coberturas": ["roubo", "furto"],
        "carencia_dias": 30,
    }
    dados.update(kw)
    return Quote(**dados)


def _present(cep_ausente: bool = False, **kw) -> Present:
    resultado = QuoteResult(
        quote_id="q_1",
        outcome=QuoteOutcome.OK,
        request=QuoteRequest(
            plano_id="completo", idade=35, veiculo_ano=2019, cep="01001000",
            data_inicio="2026-09-01",
        ),
        quote=_quote(**kw),
    )
    return Present(result=resultado, cep_ausente=cep_ausente)


# --------------------------------------------------------------------------- moeda
def test_moeda_em_formato_brasileiro_com_milhar():
    texto = render(_present(premio_mensal=1025.14, franquia=4500), ESTADO)
    assert "R$ 1.025,14/mês" in texto
    assert "R$ 4.500,00" in texto


def test_valor_apresentado_e_exatamente_o_da_api():
    texto = render(_present(premio_mensal=209.9), ESTADO)
    assert "R$ 209,90/mês" in texto


def test_nenhum_preco_esta_fixo_no_template():
    fonte = FONTE_PRESENTER.read_text(encoding="utf-8")
    assert re.search(r"R\$\s*\d", fonte) is None      # nenhum "R$ 123" literal
    assert re.search(r"\d+,\d{2}", fonte) is None     # nenhum valor em formato BRL
    assert re.search(r"\d+\.\d+", fonte) is None      # nenhum float solto


# --------------------------------------------------------------------------- Present
def test_present_traz_plano_franquia_coberturas_e_carencia():
    texto = render(_present(), ESTADO)
    assert "*Completo*" in texto
    assert "R$ 3.000,00" in texto
    assert "colisão, roubo, furto, danos a terceiros e vidros" in texto
    assert "roubo e furto" in texto
    assert "30 dias" in texto


def test_present_traduz_cobertura_tecnica():
    texto = render(_present(coberturas=["assistencia_24h", "carro_reserva"]), ESTADO)
    assert "assistência 24h" in texto
    assert "carro reserva" in texto
    assert "assistencia_24h" not in texto


def test_present_com_pro_rata_explica_o_primeiro_pagamento():
    pro_rata = ProRata(dias_no_mes=30, dias_cobrados=16, valor_primeiro_pagamento=111.95)
    texto = render(_present(pro_rata=pro_rata), ESTADO)
    assert "primeiro pagamento fica em R$ 111,95" in texto
    assert "16 dias" in texto


def test_present_sem_pro_rata_nao_fala_de_primeiro_pagamento():
    assert "primeiro pagamento" not in render(_present(), ESTADO)


def test_present_avisa_estimativa_quando_falta_cep():
    texto = render(_present(cep_ausente=True), ESTADO)
    assert "estimativa" in texto
    assert "pode subir" in texto


def test_present_sem_cep_ausente_nao_avisa():
    assert "estimativa" not in render(_present(), ESTADO)


def test_present_tem_cta_de_fechar_ou_trocar_de_plano():
    texto = render(_present(), ESTADO)
    assert "consultor" in texto
    assert "outro plano" in texto


def test_present_recusa_resultado_que_nao_e_ok():
    resultado = QuoteResult(
        quote_id="q_1",
        outcome=QuoteOutcome.INDISPONIVEL,
        request=QuoteRequest(
            plano_id="completo", idade=35, veiculo_ano=2019, data_inicio="2026-09-01"
        ),
    )
    with pytest.raises(ValueError, match="outcome OK"):
        render(Present(result=resultado), ESTADO)


# --------------------------------------------------------------------------- AskPlan
def test_ask_plan_lista_os_planos_sem_preco():
    planos = [
        PlanoResumo(id="essencial", nome="Essencial", franquia=4500, coberturas=["colisao"]),
        PlanoResumo(id="completo", nome="Completo", franquia=3000, coberturas=["colisao", "vidros"]),
        PlanoResumo(
            id="premium", nome="Premium", franquia=1500, coberturas=["colisao", "assistencia_24h"]
        ),
    ]
    texto = render(AskPlan(planos=planos), ESTADO)
    for plano in planos:
        assert f"*{plano.nome}*" in texto
    assert "R$ 4.500,00" in texto      # franquia pode aparecer
    assert "/mês" not in texto          # preço nunca
    assert "assistência 24h" in texto
    assert texto.rstrip().endswith("?")


# --------------------------------------------------------------------------- Refuse
def test_refuse_e_honesto_agradece_e_nao_oferece_humano():
    texto = render(Refuse(motivo="Idade acima do limite de aceitacao (75 anos)."), ESTADO)
    assert "não temos um plano que se encaixe no seu perfil" in texto
    assert "idade acima do limite" in texto.lower()
    assert "Agradeço" in texto
    assert "consultor" not in texto


# --------------------------------------------------------------------------- Handoff
@pytest.mark.parametrize("reason", list(HandoffReason))
def test_handoff_tem_texto_para_todo_motivo(reason: HandoffReason):
    texto = render(Handoff(reason=reason), ESTADO)
    assert texto.strip()
    assert "consultor" in texto


def test_handoff_por_motivo_e_especifico():
    aceitou = render(Handoff(reason=HandoffReason.LEAD_ACEITOU), ESTADO)
    indisponivel = render(Handoff(reason=HandoffReason.COTACAO_INDISPONIVEL), ESTADO)
    assert "finalizar" in aceitou
    assert "instável" in indisponivel     # a verdade, não "estamos analisando"
    assert aceitou != indisponivel


def test_handoff_usa_o_primeiro_nome_do_lead():
    estado = LeadState(conversation_id="conv_1", lead_nome="Ursula Souza")
    assert render(Handoff(reason=HandoffReason.LEAD_ACEITOU), estado).startswith("Ursula, ")


# --------------------------------------------------------------------------- outros
def test_confirm_cep_mostra_cidade_e_uf():
    texto = render(ConfirmCep(cep="01001000", cidade="São Paulo", uf="SP"), ESTADO)
    assert "01001-000" in texto
    assert "São Paulo/SP" in texto


def test_send_text_e_repassado_literalmente():
    assert render(SendText(text="Só um instante."), ESTADO) == "Só um instante."


@pytest.mark.parametrize(
    "action",
    [
        AskField(campo="idade"),
        Reply(directive="qualquer coisa"),
        DoQuote(
            request=QuoteRequest(
                plano_id="completo", idade=35, veiculo_ano=2019, data_inicio="2026-09-01"
            )
        ),
    ],
)
def test_acoes_do_llm_e_do_cliente_nao_sao_template(action):
    with pytest.raises(ValueError, match="não é ação de template"):
        render(action, ESTADO)
