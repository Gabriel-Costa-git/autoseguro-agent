"""Testes dos templates. Puros: nenhum preço pode nascer aqui, só vir do `Quote`."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent.models import (
    AskField,
    AskPlan,
    ConfirmCep,
    DoQuotes,
    Handoff,
    HandoffReason,
    LeadState,
    PlanoResumo,
    Present,
    PresentMany,
    ProRata,
    Quote,
    QuoteOutcome,
    QuoteRequest,
    QuoteResult,
    Refuse,
    Reply,
    SendText,
    VeiculoColetado,
)
from agent.presenter import nomes_de_coberturas, render, resumo_dos_planos
from agent.runtime_config import ConfigStore

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
        DoQuotes(
            requests=[
                QuoteRequest(
                    plano_id="completo", idade=35, veiculo_ano=2019, data_inicio="2026-09-01"
                )
            ]
        ),
    ],
)
def test_acoes_do_llm_e_do_cliente_nao_sao_template(action):
    with pytest.raises(ValueError, match="não é ação de template"):
        render(action, ESTADO)


# --------------------------------------------------------------------------- Studio (textos editáveis)
@pytest.fixture
def store_tmp(tmp_path, monkeypatch):
    """Store isolado (nunca toca `config/`) injetado no lugar do singleton do presenter."""
    loja = ConfigStore(tmp_path)
    monkeypatch.setattr("agent.presenter.store", loja)
    return loja


def test_trocar_a_versao_ativa_muda_a_cta_na_hora(store_tmp):
    assert "Quer fechar?" in render(_present(), ESTADO)
    store_tmp.add_version("presenter.present.cta", "direta", "Fecha comigo?")
    texto = render(_present(), ESTADO)
    assert texto.endswith("Fecha comigo?")
    assert "Quer fechar?" not in texto


def test_texto_novo_nao_muda_a_formatacao_do_preco(store_tmp):
    """O template recebe o valor já formatado: editar texto não consegue estragar o número."""
    store_tmp.add_version("presenter.present.preco", "sem bullet", "Fica em {premio} por mês")
    texto = render(_present(premio_mensal=1025.14), ESTADO)
    assert "Fica em R$ 1.025,14 por mês" in texto


def test_handoff_e_cobertura_tambem_saem_do_store(store_tmp):
    store_tmp.add_version("presenter.handoff.negociacao", "curta", "Vou chamar o consultor.")
    store_tmp.add_version("presenter.cobertura.vidros", "v2", "vidros e faróis")
    assert render(Handoff(reason=HandoffReason.NEGOCIACAO), ESTADO) == "Vou chamar o consultor."
    assert "vidros e faróis" in render(_present(), ESTADO)


def test_cobertura_desconhecida_cai_no_fallback(store_tmp):
    """Cobertura nova da API (sem slot) não pode quebrar a apresentação."""
    texto = render(_present(coberturas=["pneu_furado"]), ESTADO)
    assert "pneu furado" in texto


# --------------------------------------------------------------------------- PresentMany (F8)
def _resultado(outcome: QuoteOutcome = QuoteOutcome.OK, motivo: str | None = None, **kw) -> QuoteResult:
    return QuoteResult(
        quote_id="q_1",
        outcome=outcome,
        request=QuoteRequest(
            plano_id="completo", idade=35, veiculo_ano=2019, cep="01001000", data_inicio="2026-09-01",
        ),
        quote=_quote(**kw) if outcome is QuoteOutcome.OK else None,
        motivo_recusa=motivo,
    )


def _carro(texto: str, ano: int, **kw) -> VeiculoColetado:
    return VeiculoColetado(texto=texto, ano=ano, quote_result=_resultado(**kw))


def test_present_many_traz_um_bloco_por_carro_e_um_cta_so():
    acao = PresentMany(
        resultados=[
            _carro("Onix 2022", 2022, premio_mensal=209.9),
            _carro("HB20 2020", 2020, premio_mensal=189.5),
        ]
    )
    texto = render(acao, ESTADO)

    assert texto.startswith("Cotei os 2 carros no plano *Completo*:")
    assert "*Onix 2022*" in texto and "*HB20 2020*" in texto
    assert "R$ 209,90/mês" in texto and "R$ 189,50/mês" in texto
    assert texto.count("Quer fechar?") == 1          # um fechamento, não um por carro


def test_present_many_cita_recusado_e_pendente_sem_preco():
    acao = PresentMany(
        resultados=[
            _carro("Onix 2022", 2022, premio_mensal=209.9),
            _carro("Fusca 1980", 1980, outcome=QuoteOutcome.RECUSA, motivo="Veículo com mais de 20 anos não é aceito."),
            _carro("Gol 2015", 2015, outcome=QuoteOutcome.INDISPONIVEL),
        ]
    )
    texto = render(acao, ESTADO)

    assert "*Fusca 1980*: não consigo cotar — veículo com mais de 20 anos não é aceito." in texto
    assert "em seguida" in texto and "Gol 2015" in texto
    assert texto.count("/mês") == 1                  # só o carro que cotou tem preço


def test_present_many_sem_nenhum_ok_e_erro_de_programacao():
    acao = PresentMany(resultados=[_carro("Gol 2015", 2015, outcome=QuoteOutcome.INDISPONIVEL)])
    with pytest.raises(ValueError, match="pelo menos uma cotação OK"):
        render(acao, ESTADO)


def test_present_many_avisa_estimativa_quando_falta_cep():
    acao = PresentMany(resultados=[_carro("Onix 2022", 2022)], cep_ausente=True)
    assert "estimativa" in render(acao, ESTADO)


def test_cta_muda_quando_o_plano_foi_assumido():
    estado = LeadState(conversation_id="conv_1", plano_assumido=True)
    texto_um = render(_present(), estado)
    texto_varios = render(PresentMany(resultados=[_carro("Onix 2022", 2022)]), estado)

    for texto in (texto_um, texto_varios):
        assert "prefere ver o Completo ou o Premium?" in texto
        assert "Quer fechar?" not in texto


def test_um_carro_continua_com_o_texto_de_sempre():
    """O `Present` de 1 carro é o entregue: mesmo corpo, mesmo CTA (goldens intactos)."""
    texto = render(_present(), ESTADO)
    assert texto.startswith("Cotei aqui o plano *Completo*:")
    assert texto.endswith("Quer fechar? Um consultor finaliza com você. Ou prefere ver outro plano?")


# --------------------------------------------------------------------------- dados dos planos (F10)
def _planos() -> list[PlanoResumo]:
    return [
        PlanoResumo(id="essencial", nome="Essencial", franquia=4500, coberturas=["colisao", "roubo", "furto"]),
        PlanoResumo(id="completo", nome="Completo", franquia=3000,
                    coberturas=["colisao", "roubo", "furto", "terceiros", "vidros"]),
        PlanoResumo(id="premium", nome="Premium", franquia=1500,
                    coberturas=["colisao", "carro_reserva", "assistencia_24h"]),
    ]


def test_resumo_dos_planos_tem_franquia_e_coberturas_sem_preco():
    texto = resumo_dos_planos(_planos())
    assert "Essencial" in texto and "Completo" in texto and "Premium" in texto
    assert "franquia de R$ 4.500,00" in texto
    assert "colisão, roubo e furto" in texto              # nomes legíveis, não as chaves
    assert "carro reserva" in texto and "carro_reserva" not in texto
    assert "/mês" not in texto and "premio" not in texto  # preço não entra: só sai da cotação


def test_resumo_dos_planos_inclui_a_carencia_quando_existe():
    texto = resumo_dos_planos(_planos(), {"coberturas": ["roubo", "furto"], "dias": 30})
    assert "carência: roubo e furto só passam a valer 30 dias" in texto


def test_resumo_dos_planos_sem_carencia_nao_inventa():
    assert "carência" not in resumo_dos_planos(_planos())


def test_nomes_de_coberturas_nao_repete_e_traduz():
    assert nomes_de_coberturas(["colisao", "roubo", "colisao"]) == ["colisão", "roubo"]
    assert nomes_de_coberturas(["cobertura_nova"]) == ["cobertura nova"]   # fallback sem quebrar

