"""Testes da camada LLM: montagem dos prompts e guardrail anti-preço (sem rede, sem chave)."""
from __future__ import annotations

from datetime import date

from agent.brain import (
    FALLBACK_PADRAO,
    FALLBACKS,
    build_extraction_instructions,
    build_responder_instructions,
    contem_preco,
    directive_for_field,
    guard_price,
    resumo_state,
)
from agent.models import CepInfo, LeadState, Stage
from tests.fakes import quote_indisponivel, quote_ok

HOJE = date(2026, 9, 1)


def _state(**over) -> LeadState:
    base = {"conversation_id": "c1"}
    base.update(over)
    return LeadState(**base)


# --------------------------------------------------------------------------- prompt do Extractor
def test_prompt_extracao_traz_hoje_e_ano_corrente():
    prompt = build_extraction_instructions(_state(), HOJE)
    assert "2026-09-01" in prompt
    assert "2026" in prompt
    assert "ano_parece_modelo" in prompt


def test_prompt_extracao_traz_ultima_pergunta_para_desambiguar():
    prompt = build_extraction_instructions(_state(ultima_pergunta="idade"), HOJE)
    assert "idade" in prompt
    assert "Última pergunta" in prompt


def test_prompt_extracao_lista_intents_com_exemplos():
    prompt = build_extraction_instructions(_state(), HOJE)
    for intent in ("pedir_humano", "objecao_preco", "fora_de_escopo", "aceitar"):
        assert intent in prompt
    assert "tá caro" in prompt
    assert "atendente" in prompt
    assert "bati o carro" in prompt
    assert "pode emitir" in prompt


def test_prompt_extracao_manda_extrair_so_a_mensagem_atual():
    prompt = build_extraction_instructions(_state(idade=35), HOJE)
    assert "ATUAL" in prompt
    assert "data_vaga" in prompt


# --------------------------------------------------------------------------- prompt do Responder
def test_prompt_responder_tem_persona_diretiva_e_regras_duras():
    prompt = build_responder_instructions(_state(idade=35), "peça o CEP de onde o carro dorme")
    assert "AutoSeguro" in prompt and "WhatsApp" in prompt
    assert "peça o CEP de onde o carro dorme" in prompt
    assert "NUNCA cite preço" in prompt
    assert "CPF" in prompt and "placa" in prompt
    assert "desconto" in prompt


def test_prompt_responder_nunca_carrega_valor_da_cotacao():
    """O resumo do estado só diz o status da cotação: o número nunca entra no prompt."""
    state = _state(stage=Stage.APRESENTADO, quote_result=quote_ok())
    prompt = build_responder_instructions(state, "responda à dúvida do lead")
    assert "209" not in prompt
    assert "3000" not in prompt
    assert "ok" in prompt


def test_resumo_state_mostra_dados_coletados_sem_numero_de_preco():
    state = _state(
        idade=35,
        veiculo_texto="Onix",
        veiculo_ano=2019,
        cep="01310100",
        cep_info=CepInfo(cep="01310100", existe=True, cidade="São Paulo", uf="SP"),
        plano_id="completo",
    )
    resumo = resumo_state(state)
    assert "35" in resumo and "Onix" in resumo and "01310100" in resumo and "São Paulo/SP" in resumo


def test_resumo_state_marca_cep_ausente():
    assert "não sabe" in resumo_state(_state(cep_ausente=True))


# --------------------------------------------------------------------------- diretivas
def test_directive_for_field_por_campo_e_com_motivo():
    assert "CEP" in directive_for_field("cep")
    com_motivo = directive_for_field("veiculo", motivo="ano 2027 parece ano-modelo")
    assert "ano 2027 parece ano-modelo" in com_motivo
    assert "ano de fabricação" in com_motivo


# --------------------------------------------------------------------------- guardrail
def test_contem_preco_pega_dinheiro_e_ignora_ano_e_idade():
    assert contem_preco("fica R$ 189 por mês")
    assert contem_preco("dá 209,90 na mensalidade")
    assert contem_preco("uns 200 reais")
    assert not contem_preco("seu Onix 2019, 35 anos, cep 01310100")


def test_guard_price_troca_resposta_com_preco_por_fallback_do_campo():
    state = _state(ultima_pergunta="cep")
    saida = guard_price("Fica uns R$ 180,00 por mês. Qual seu CEP?", state)
    assert saida == FALLBACKS["cep"]
    assert not contem_preco(saida)


def test_guard_price_usa_fallback_padrao_sem_pergunta_pendente():
    assert guard_price("uns 250,00 talvez", _state()) == FALLBACK_PADRAO


def test_guard_price_dispara_mesmo_com_cotacao_indisponivel():
    state = _state(ultima_pergunta="idade", quote_result=quote_indisponivel())
    assert guard_price("deve dar uns R$ 300", state) == FALLBACKS["idade"]


def test_guard_price_libera_quando_a_cotacao_veio_da_api():
    """Com cotação OK o valor já é público (veio do presenter): o LLM pode conversar sobre ele."""
    state = _state(ultima_pergunta="plano", quote_result=quote_ok())
    texto = "O valor de R$ 209,90 que te passei já inclui assistência."
    assert guard_price(texto, state) == texto


def test_guard_price_nao_mexe_em_texto_limpo():
    texto = "Perfeito! Qual o ano de fabricação do carro?"
    assert guard_price(texto, _state(ultima_pergunta="veiculo")) == texto


def test_fallbacks_nao_contem_preco():
    for texto in [*FALLBACKS.values(), FALLBACK_PADRAO]:
        assert not contem_preco(texto)
