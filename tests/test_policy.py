"""Testes da máquina de estados. Puros: sem rede, sem LLM, sem docker."""
from __future__ import annotations

from datetime import date

import pytest

from agent.config import settings
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
    Intent,
    LeadState,
    PlanoId,
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
    Stage,
    VeiculoColetado,
    VeiculoExtraido,
    Violation,
)
from agent.policy import DIRETIVA_POS_COTACAO, TXT_INSTABILIDADE, TXT_MIDIA, next_action
from agent.runtime_config import ConfigStore

HOJE = date(2026, 9, 1)


class FakeRules:
    """Espelha o contrato de `rules.Rules` (idade 18–75, veículo 2006–2026 para HOJE)."""

    def validate_idade(self, idade: int) -> Violation | None:
        if idade < 18 or idade > 75:
            return Violation(
                campo="idade",
                tipo="fora_da_faixa",
                motivo="aceitamos condutores de 18 a 75 anos",
            )
        return None

    def validate_veiculo_ano(self, ano: int) -> Violation | None:
        if ano > HOJE.year:
            return Violation(
                campo="veiculo_ano", tipo="futuro", motivo="o ano de fabricação está no futuro"
            )
        if ano < 2006:
            return Violation(
                campo="veiculo_ano",
                tipo="fora_da_faixa",
                motivo="não aceitamos veículos com mais de 20 anos",
            )
        return None

    def validate_data_inicio(self, d: date) -> Violation | None:
        if d < HOJE:
            return Violation(campo="data_inicio", tipo="passado", motivo="a data já passou")
        return None

    def normalize_cep(self, texto: str) -> str | None:
        digitos = "".join(c for c in texto if c.isdigit())
        return digitos if len(digitos) == 8 else None

    def validate_request(self, req: QuoteRequest) -> list[Violation]:
        return []

    def planos_resumo(self) -> list[PlanoResumo]:
        return [
            PlanoResumo(
                id="essencial", nome="Essencial", franquia=4500, coberturas=["colisao", "roubo"]
            ),
            PlanoResumo(
                id="completo", nome="Completo", franquia=3000, coberturas=["colisao", "vidros"]
            ),
            PlanoResumo(
                id="premium", nome="Premium", franquia=1500, coberturas=["colisao", "carro_reserva"]
            ),
        ]


RULES = FakeRules()


# --------------------------------------------------------------------------- fábricas
def _state(**kw) -> LeadState:
    return LeadState(conversation_id="conv_1", **kw)


def _extr(**kw) -> Extraction:
    kw.setdefault("intent", Intent.FORNECER_DADOS)
    return Extraction(**kw)


def _quote(pro_rata: ProRata | None = None) -> Quote:
    return Quote(
        plano_id="completo",
        plano_nome="Completo",
        premio_mensal=209.9,
        franquia=3000,
        coberturas=["colisao", "roubo", "furto"],
        multiplicadores={"faixa_etaria": 1.0},
        carencia_coberturas=["roubo", "furto"],
        carencia_dias=30,
        pro_rata=pro_rata,
    )


def _result(outcome: QuoteOutcome = QuoteOutcome.OK, **kw) -> QuoteResult:
    dados = {
        "quote_id": "q_1",
        "outcome": outcome,
        "request": QuoteRequest(
            plano_id="completo", idade=35, veiculo_ano=2019, cep="01001000",
            data_inicio=HOJE.isoformat(),
        ),
        "quote": _quote() if outcome is QuoteOutcome.OK else None,
    }
    dados.update(kw)
    return QuoteResult(**dados)


def _completo(**kw) -> LeadState:
    """Estado com tudo coletado, pronto para cotar."""
    base = {
        "idade": 35,
        "veiculo_ano": 2019,
        "veiculo_texto": "Onix 2019",
        "cep": "01001000",
        "cep_confirmado": True,
        "plano_id": "completo",
    }
    base.update(kw)
    return _state(**base)


def _act(state: LeadState, extraction: Extraction | None):
    return next_action(state, extraction, RULES, HOJE)


# --------------------------------------------------------------------------- pureza
def test_state_de_entrada_nao_e_mutado():
    entrada = _state(stage=Stage.COLETA_IDADE)
    antes = entrada.model_dump()
    novo, _ = _act(entrada, _extr(idade=40))
    assert entrada.model_dump() == antes
    assert novo is not entrada
    assert novo.idade == 40


# --------------------------------------------------------------------------- caminho feliz
def test_caminho_feliz_turno_a_turno():
    s = _state()

    s, acoes = _act(s, _extr(intent=Intent.SAUDACAO))
    assert acoes == [AskField(campo="idade")]
    assert s.stage is Stage.COLETA_IDADE

    # F8: o plano é a 2ª pergunta — ele vale para todos os carros do lead
    s, acoes = _act(s, _extr(idade=35))
    assert isinstance(acoes[0], AskPlan)
    assert len(acoes[0].planos) == 3
    assert s.stage is Stage.ESCOLHA_PLANO

    s, acoes = _act(s, _extr(intent=Intent.ESCOLHER_PLANO, plano_id="completo"))
    assert acoes == [AskField(campo="veiculo", motivo=None)]
    assert s.stage is Stage.COLETA_VEICULO
    assert s.plano_assumido is False

    s, acoes = _act(s, _extr(veiculo_texto="Onix 2019", veiculo_ano=2019))
    assert acoes == [AskField(campo="cep", motivo=None)]
    assert s.veiculo_texto == "Onix 2019"
    assert [(v.texto, v.ano) for v in s.veiculos] == [("Onix 2019", 2019)]

    s, acoes = _act(s, _extr(cep="01001-000"))
    assert acoes == []  # aguardando o lookup do conversation
    assert s.stage is Stage.CONFIRMA_CEP
    assert s.cep == "01001000"

    s.cep_info = CepInfo(cep="01001000", existe=True, cidade="São Paulo", uf="SP")
    s, acoes = _act(s, None)
    assert acoes == [ConfirmCep(cep="01001000", cidade="São Paulo", uf="SP")]

    s, acoes = _act(s, _extr(intent=Intent.CONFIRMAR))
    assert acoes == [
        DoQuotes(
            requests=[
                QuoteRequest(
                    plano_id="completo", idade=35, veiculo_ano=2019, cep="01001000",
                    data_inicio="2026-09-01",
                )
            ]
        )
    ]
    assert s.stage is Stage.COTANDO


def test_dado_fora_de_ordem_e_absorvido():
    s, acoes = _act(_state(), _extr(idade=35, veiculo_ano=2019, plano_id="premium"))
    assert acoes == [AskField(campo="cep")]
    assert (s.idade, s.veiculo_ano, s.plano_id) == (35, 2019, "premium")


# --------------------------------------------------------------------------- recusas e correções
def test_idade_acima_do_limite_recusa_sem_handoff():
    s, acoes = _act(_state(stage=Stage.COLETA_IDADE), _extr(idade=76))
    assert isinstance(acoes[0], Refuse)
    assert s.stage is Stage.ENCERRADO_RECUSA
    assert s.handoff_reason is None


def test_idade_menor_de_18_recusa():
    s, acoes = _act(_state(), _extr(idade=17))
    assert isinstance(acoes[0], Refuse)
    assert s.stage is Stage.ENCERRADO_RECUSA


def test_ano_futuro_pergunta_de_novo_sem_gravar():
    s, acoes = _act(_state(idade=35), _extr(veiculo_ano=2027, veiculo_texto="Onix 2027"))
    assert acoes == [
        AskField(campo="veiculo", motivo="ano futuro parece ano-modelo; confirmar ano de fabricação")
    ]
    assert s.veiculo_ano is None
    assert s.stage is Stage.COLETA_VEICULO


def test_ano_parece_modelo_pergunta_de_novo():
    _, acoes = _act(_state(idade=35), _extr(veiculo_ano=2026, ano_parece_modelo=True))
    assert isinstance(acoes[0], AskField)
    assert acoes[0].campo == "veiculo"


def test_veiculo_antigo_demais_recusa():
    s, acoes = _act(_state(idade=35), _extr(veiculo_ano=2005))
    assert isinstance(acoes[0], Refuse)
    assert s.stage is Stage.ENCERRADO_RECUSA


# --------------------------------------------------------------------------- CEP
def test_cep_invalido_pede_de_novo_e_conta_tentativa():
    s, acoes = _act(_state(idade=35, veiculo_ano=2019, stage=Stage.COLETA_CEP), _extr(cep="123"))
    assert acoes == [AskField(campo="cep", motivo="formato inválido")]
    assert s.cep_tentativas == 1
    assert s.cep is None


def test_cep_invalido_tres_vezes_segue_sem_cep():
    s = _state(idade=35, veiculo_ano=2019, stage=Stage.COLETA_CEP)
    for _ in range(settings.max_cep_tentativas):
        s, acoes = _act(s, _extr(cep="abc"))
        assert isinstance(acoes[0], AskField)
    s, acoes = _act(s, _extr(cep="abc"))
    assert isinstance(acoes[0], AskPlan)
    assert s.cep_ausente is True


def test_cep_inexistente_pede_de_novo_e_depois_segue_com_o_informado():
    s = _state(
        idade=35,
        veiculo_ano=2019,
        cep="99999999",
        stage=Stage.CONFIRMA_CEP,
        cep_info=CepInfo(cep="99999999", existe=False),
    )
    for _ in range(settings.max_cep_tentativas):
        s, acoes = _act(s, None)
        assert acoes == [AskField(campo="cep", motivo="não encontrei esse CEP; pedir de novo")]
        s.stage = Stage.CONFIRMA_CEP  # o conversation re-chama após novo lookup
    s, acoes = _act(s, None)
    assert isinstance(acoes[0], AskPlan)
    assert s.cep == "99999999"
    assert s.cep_ausente is False


def test_viacep_indisponivel_segue_sem_confirmar():
    s = _state(
        idade=35,
        veiculo_ano=2019,
        cep="01001000",
        stage=Stage.CONFIRMA_CEP,
        cep_info=CepInfo(cep="01001000", existe=None),
    )
    s, acoes = _act(s, None)
    assert not any(isinstance(a, ConfirmCep) for a in acoes)
    assert isinstance(acoes[0], AskPlan)
    assert s.cep_confirmado is True


def test_lead_nega_o_cep_confirmado():
    s = _state(
        idade=35,
        veiculo_ano=2019,
        cep="01001000",
        stage=Stage.CONFIRMA_CEP,
        cep_info=CepInfo(cep="01001000", existe=True, cidade="São Paulo", uf="SP"),
    )
    s, acoes = _act(s, _extr(intent=Intent.NEGAR))
    assert isinstance(acoes[0], AskField)
    assert acoes[0].campo == "cep"
    assert s.cep is None
    assert s.cep_tentativas == 1


def test_nao_sei_o_cep_segue_como_ausente():
    s, acoes = _act(
        _state(idade=35, veiculo_ano=2019, stage=Stage.COLETA_CEP), _extr(intent=Intent.NAO_SEI)
    )
    assert isinstance(acoes[0], AskPlan)
    assert s.cep_ausente is True


def test_cotacao_sem_cep_vai_sem_cep_no_request():
    _, acoes = _act(
        _completo(cep=None, cep_confirmado=False, cep_ausente=True, plano_id=None),
        _extr(intent=Intent.ESCOLHER_PLANO, plano_id="completo"),
    )
    assert isinstance(acoes[0], DoQuotes)
    assert acoes[0].requests[0].cep is None


# --------------------------------------------------------------------------- data de início
def test_data_no_passado_avisa_e_nao_grava():
    s, acoes = _act(_state(idade=35), _extr(data_inicio=date(2026, 1, 1)))
    assert isinstance(acoes[0], SendText)
    assert s.data_inicio is None


def test_data_futura_vai_para_a_cotacao():
    s, acoes = _act(_completo(plano_id=None), _extr(plano_id="completo", data_inicio=date(2026, 10, 1)))
    assert isinstance(acoes[0], DoQuotes)
    assert acoes[0].requests[0].data_inicio == "2026-10-01"
    assert s.data_inicio == date(2026, 10, 1)


# --------------------------------------------------------------------------- saídas
@pytest.mark.parametrize(
    "stage",
    [Stage.INICIO, Stage.COLETA_IDADE, Stage.CONFIRMA_CEP, Stage.ESCOLHA_PLANO, Stage.APRESENTADO],
)
def test_pedir_humano_em_qualquer_estagio(stage: Stage):
    s, acoes = _act(_state(stage=stage), _extr(intent=Intent.PEDIR_HUMANO))
    assert acoes[0] == Handoff(
        reason=HandoffReason.LEAD_PEDIU_HUMANO, payload=acoes[0].payload
    )
    assert s.stage is Stage.HANDOFF
    assert s.handoff_reason is HandoffReason.LEAD_PEDIU_HUMANO


def test_fora_de_escopo_vai_para_humano():
    _, acoes = _act(_state(stage=Stage.COLETA_CEP), _extr(intent=Intent.FORA_DE_ESCOPO))
    assert isinstance(acoes[0], Handoff)
    assert acoes[0].reason is HandoffReason.FORA_DE_ESCOPO


def test_recusar_encerra_com_despedida():
    s, acoes = _act(_state(stage=Stage.COLETA_VEICULO), _extr(intent=Intent.RECUSAR))
    assert isinstance(acoes[0], SendText)
    assert s.stage is Stage.ENCERRADO


def test_midia_sem_texto_pede_texto():
    s, acoes = _act(_state(), None)
    assert isinstance(acoes[0], SendText)
    assert "escrever" in acoes[0].text
    assert s.stage is Stage.INICIO


def test_sem_progresso_escala_para_humano():
    s = _state()
    for _ in range(settings.max_turnos_sem_progresso - 1):
        s, acoes = _act(s, _extr(intent=Intent.OUTRO))
        assert isinstance(acoes[0], AskField)
    s, acoes = _act(s, _extr(intent=Intent.OUTRO))
    assert isinstance(acoes[0], Handoff)
    assert acoes[0].reason is HandoffReason.SEM_PROGRESSO


def test_dado_novo_zera_o_contador_de_estagnacao():
    s, _ = _act(_state(), _extr(intent=Intent.SAUDACAO))
    assert s.turnos_sem_progresso == 1
    s, _ = _act(s, _extr(idade=35))
    assert s.turnos_sem_progresso == 0
    assert s.turnos == 2


@pytest.mark.parametrize("stage", [Stage.HANDOFF, Stage.ENCERRADO, Stage.ENCERRADO_RECUSA])
def test_estados_terminais_nao_reabrem_coleta(stage: Stage):
    s, acoes = _act(_state(stage=stage), _extr(idade=35))
    assert len(acoes) == 1
    assert isinstance(acoes[0], SendText)
    assert s.stage is stage
    assert s.idade is None


# --------------------------------------------------------------------------- pós-cotação
def test_pos_cotacao_ok_apresenta():
    s, acoes = _act(_completo(stage=Stage.COTANDO, quote_result=_result()), None)
    assert isinstance(acoes[0], Present)
    assert acoes[0].cep_ausente is False
    assert s.stage is Stage.APRESENTADO


def test_pos_cotacao_ok_propaga_cep_ausente():
    estado = _completo(
        stage=Stage.COTANDO, quote_result=_result(), cep=None, cep_ausente=True,
        cep_confirmado=False,
    )
    _, acoes = _act(estado, None)
    assert acoes[0].cep_ausente is True


def test_pos_cotacao_recusa_usa_motivo_da_api():
    resultado = _result(QuoteOutcome.RECUSA, motivo_recusa="Veiculo com mais de 20 anos.")
    s, acoes = _act(_completo(stage=Stage.COTANDO, quote_result=resultado), None)
    assert acoes[0] == Refuse(motivo="Veiculo com mais de 20 anos.")
    assert s.stage is Stage.ENCERRADO_RECUSA


def test_pos_cotacao_bug_vira_erro_interno():
    resultado = _result(QuoteOutcome.BUG, erro="422 detail")
    s, acoes = _act(_completo(stage=Stage.COTANDO, quote_result=resultado), None)
    assert acoes[0].reason is HandoffReason.ERRO_INTERNO
    assert s.stage is Stage.HANDOFF


def test_pos_cotacao_indisponivel_leva_dados_para_o_humano():
    resultado = _result(QuoteOutcome.INDISPONIVEL, erro="esgotou tentativas")
    _, acoes = _act(_completo(stage=Stage.COTANDO, quote_result=resultado), None)
    handoff = acoes[0]
    assert handoff.reason is HandoffReason.COTACAO_INDISPONIVEL
    assert handoff.payload["dados"]["idade"] == 35
    assert handoff.payload["dados"]["veiculo_ano"] == 2019
    assert handoff.payload["conversation_id"] == "conv_1"


# --------------------------------------------------------------------------- pós-apresentação
def test_aceitar_gera_handoff_com_cotacao_e_quote_id():
    estado = _completo(stage=Stage.APRESENTADO, quote_result=_result())
    s, acoes = _act(estado, _extr(intent=Intent.ACEITAR))
    handoff = acoes[0]
    assert handoff.reason is HandoffReason.LEAD_ACEITOU
    assert handoff.payload["quote_id"] == "q_1"
    assert handoff.payload["cotacao"]["quote"]["premio_mensal"] == 209.9
    assert s.stage is Stage.HANDOFF


def test_primeira_objecao_de_preco_e_reply_sem_valores():
    estado = _completo(stage=Stage.APRESENTADO, quote_result=_result())
    s, acoes = _act(estado, _extr(intent=Intent.OBJECAO_PRECO))
    assert isinstance(acoes[0], Reply)
    assert "desconto" in acoes[0].directive
    assert s.objecoes == 1
    assert s.stage is Stage.APRESENTADO


def test_segunda_objecao_vira_negociacao():
    estado = _completo(stage=Stage.APRESENTADO, quote_result=_result(), objecoes=1)
    s, acoes = _act(estado, _extr(intent=Intent.OBJECAO_PRECO))
    assert acoes[0].reason is HandoffReason.NEGOCIACAO
    assert s.stage is Stage.HANDOFF


def test_pedido_explicito_de_desconto_vai_direto_para_negociacao():
    estado = _completo(stage=Stage.APRESENTADO, quote_result=_result())
    _, acoes = _act(
        estado, _extr(intent=Intent.OBJECAO_PRECO, observacao="pediu desconto de 10%")
    )
    assert acoes[0].reason is HandoffReason.NEGOCIACAO


@pytest.mark.parametrize("novo_plano", ["essencial", "premium"])
def test_troca_de_plano_recota(novo_plano: PlanoId):
    estado = _completo(stage=Stage.APRESENTADO, quote_result=_result())
    s, acoes = _act(estado, _extr(intent=Intent.ESCOLHER_PLANO, plano_id=novo_plano))
    assert isinstance(acoes[0], DoQuotes)
    assert acoes[0].requests[0].plano_id == novo_plano
    assert s.stage is Stage.COTANDO
    assert s.quote_result is None  # resultado antigo descartado


def test_mensagem_solta_apos_cotacao_devolve_reply():
    estado = _completo(stage=Stage.APRESENTADO, quote_result=_result())
    s, acoes = _act(estado, _extr(intent=Intent.OUTRO))
    assert isinstance(acoes[0], Reply)
    assert s.stage is Stage.APRESENTADO


def test_mensagem_durante_a_cotacao_pede_paciencia():
    _, acoes = _act(_completo(stage=Stage.COTANDO), _extr(intent=Intent.OUTRO))
    assert isinstance(acoes[0], SendText)


def test_present_com_pro_rata_preserva_o_resultado_da_api():
    resultado = _result()
    resultado.quote = _quote(
        pro_rata=ProRata(dias_no_mes=30, dias_cobrados=16, valor_primeiro_pagamento=111.95)
    )
    _, acoes = _act(_completo(stage=Stage.COTANDO, quote_result=resultado), None)
    assert acoes[0].result.quote.pro_rata.valor_primeiro_pagamento == 111.95




def test_pedir_desconto_apresentado_vai_direto_para_negociacao():
    """Desconto é decisão comercial: 1 pedido explícito já escala, sem passar pela objeção."""
    s, acoes = _act(_completo(stage=Stage.APRESENTADO, quote_result=_result()), _extr(intent=Intent.PEDIR_DESCONTO))
    assert s.stage is Stage.HANDOFF and s.handoff_reason is HandoffReason.NEGOCIACAO
    assert isinstance(acoes[0], Handoff)


# --------------------------------------------------------------------------- extração indisponível
def _indisponivel(**kw) -> Extraction:
    """Como o brain marca quando o LLM falha de vez (cota/rede/parse)."""
    kw.setdefault("intent", Intent.OUTRO)
    kw.setdefault("observacao", "extracao_indisponivel")
    return Extraction(indisponivel=True, **kw)


def test_indisponivel_em_escolha_plano_nao_repete_a_lista_de_planos():
    """Regressão do log demo-feliz-01: o lead recebeu o AskPlan 3x seguidas."""
    s, acoes = _act(_completo(stage=Stage.ESCOLHA_PLANO, plano_id=None), _indisponivel())
    assert acoes == [SendText(text=TXT_INSTABILIDADE)]
    assert not any(isinstance(a, AskPlan) for a in acoes)
    assert s.stage is Stage.ESCOLHA_PLANO


def test_indisponivel_em_coleta_cep_nao_repete_a_pergunta():
    s, acoes = _act(
        _state(idade=35, veiculo_ano=2019, stage=Stage.COLETA_CEP, ultima_pergunta="cep"),
        _indisponivel(),
    )
    assert acoes == [SendText(text=TXT_INSTABILIDADE)]
    assert not any(isinstance(a, AskField) for a in acoes)
    assert s.stage is Stage.COLETA_CEP


def test_indisponivel_em_apresentado_pede_para_repetir_sem_escalar():
    estado = _completo(stage=Stage.APRESENTADO, quote_result=_result())
    s, acoes = _act(estado, _indisponivel())
    assert acoes == [SendText(text=TXT_INSTABILIDADE)]
    assert s.stage is Stage.APRESENTADO
    assert s.handoff_reason is None


def test_indisponivel_nao_absorve_nem_obedece_o_que_veio_junto():
    """Se o LLM falhou, o conteúdo da extração não é confiável — nem dados, nem intent."""
    s, acoes = _act(
        _state(stage=Stage.COLETA_IDADE),
        _indisponivel(intent=Intent.PEDIR_HUMANO, idade=99, plano_id="premium"),
    )
    assert acoes == [SendText(text=TXT_INSTABILIDADE)]
    assert s.idade is None
    assert s.plano_id is None
    assert s.stage is Stage.COLETA_IDADE


def test_indisponivel_repetido_escala_para_humano():
    s = _completo(stage=Stage.ESCOLHA_PLANO, plano_id=None)
    for _ in range(settings.max_turnos_sem_progresso - 1):
        s, acoes = _act(s, _indisponivel())
        assert isinstance(acoes[0], SendText)
    s, acoes = _act(s, _indisponivel())
    assert isinstance(acoes[0], Handoff)
    assert acoes[0].reason is HandoffReason.SEM_PROGRESSO
    assert s.stage is Stage.HANDOFF


@pytest.mark.parametrize("stage", [Stage.HANDOFF, Stage.ENCERRADO, Stage.ENCERRADO_RECUSA])
def test_indisponivel_em_estado_terminal_mantem_a_resposta_terminal(stage: Stage):
    _, acoes = _act(_state(stage=stage), _indisponivel())
    assert isinstance(acoes[0], SendText)
    assert acoes[0].text != TXT_INSTABILIDADE


def test_intent_outro_sem_indisponivel_mantem_o_comportamento_antigo():
    _, acoes = _act(
        _completo(stage=Stage.ESCOLHA_PLANO, plano_id=None), _extr(intent=Intent.OUTRO)
    )
    assert isinstance(acoes[0], AskPlan)


# --------------------------------------------------------------------------- Studio (config editável)
@pytest.fixture
def store_tmp(tmp_path, monkeypatch):
    """Store isolado (nunca toca `config/`) injetado no lugar do singleton da policy."""
    loja = ConfigStore(tmp_path)
    monkeypatch.setattr("agent.policy.store", loja)
    return loja


def test_trocar_a_versao_ativa_muda_o_texto_na_hora(store_tmp):
    _, antes = _act(_state(), None)
    assert antes[0].text == TXT_MIDIA

    store_tmp.add_version("policy.txt_midia", "seca", "Me manda por escrito, por favor.")
    _, depois = _act(_state(), None)
    assert depois[0].text == "Me manda por escrito, por favor."
    assert TXT_MIDIA != depois[0].text  # a constante continua sendo o texto entregue


def test_diretiva_de_objecao_tambem_vem_do_slot(store_tmp):
    store_tmp.add_version("policy.diretiva_objecao", "v2", "diga que o preço é o da tabela")
    estado = _completo(stage=Stage.APRESENTADO, quote_result=_result())
    _, acoes = _act(estado, _extr(intent=Intent.OBJECAO_PRECO))
    assert acoes[0] == Reply(directive="diga que o preço é o da tabela")


def test_max_cep_tentativas_zero_segue_sem_cep_na_primeira_falha(store_tmp):
    store_tmp.set_overrides("tools", {"policy": {"max_cep_tentativas": 0}})
    s, acoes = _act(_state(idade=35, veiculo_ano=2019, stage=Stage.COLETA_CEP), _extr(cep="abc"))
    assert isinstance(acoes[0], AskPlan)
    assert s.cep_ausente is True


def test_max_turnos_sem_progresso_um_escala_no_primeiro_turno_parado(store_tmp):
    store_tmp.set_overrides("tools", {"policy": {"max_turnos_sem_progresso": 1}})
    _, acoes = _act(_state(), _extr(intent=Intent.SAUDACAO))
    assert acoes[0].reason is HandoffReason.SEM_PROGRESSO


def test_objecoes_ate_handoff_um_escala_na_primeira_objecao(store_tmp):
    store_tmp.set_overrides("tools", {"policy": {"objecoes_ate_handoff": 1}})
    estado = _completo(stage=Stage.APRESENTADO, quote_result=_result())
    _, acoes = _act(estado, _extr(intent=Intent.OBJECAO_PRECO))
    assert acoes[0].reason is HandoffReason.NEGOCIACAO


def _sem_pre_validacao(loja: ConfigStore) -> None:
    loja.set_overrides("tools", {"rules": {"pre_validacao_local": False}})


def test_sem_pre_validacao_local_idade_fora_da_faixa_nao_recusa(store_tmp):
    """Toggle desligado: quem recusa é a API (422), não a gente."""
    _sem_pre_validacao(store_tmp)
    s, acoes = _act(_state(stage=Stage.COLETA_IDADE), _extr(idade=80))
    assert not any(isinstance(a, Refuse) for a in acoes)
    assert s.idade == 80
    assert isinstance(acoes[0], AskPlan)      # F8: plano é a pergunta seguinte à idade


def test_sem_pre_validacao_local_veiculo_antigo_vai_para_a_api(store_tmp):
    _sem_pre_validacao(store_tmp)
    s, acoes = _act(
        _state(idade=35, plano_id="completo"), _extr(veiculo_ano=2005, veiculo_texto="Corolla 2005")
    )
    assert not any(isinstance(a, Refuse) for a in acoes)
    assert s.veiculo_ano == 2005
    assert acoes == [AskField(campo="cep", motivo=None)]


def test_sem_pre_validacao_local_ano_futuro_ainda_pergunta(store_tmp):
    """Ano-modelo é UX de coleta, não regra de negócio: continua sendo confirmado."""
    _sem_pre_validacao(store_tmp)
    s, acoes = _act(_state(idade=35), _extr(veiculo_ano=2027))
    assert isinstance(acoes[0], AskField)
    assert acoes[0].campo == "veiculo"
    assert s.veiculo_ano is None


def test_sem_pre_validacao_local_data_passada_e_aceita(store_tmp):
    _sem_pre_validacao(store_tmp)
    s, acoes = _act(_state(idade=35), _extr(data_inicio=date(2026, 1, 1)))
    assert not any(isinstance(a, SendText) for a in acoes)
    assert s.data_inicio == date(2026, 1, 1)


def test_com_pre_validacao_local_o_comportamento_entregue_continua(store_tmp):
    """Sem override, o default é ligado — a recusa local acontece como na entrega."""
    s, acoes = _act(_state(stage=Stage.COLETA_IDADE), _extr(idade=80))
    assert isinstance(acoes[0], Refuse)
    assert s.stage is Stage.ENCERRADO_RECUSA


# --------------------------------------------------------------------------- consulta com ferramenta
# `Intent.CONSULTA` só chega à policy quando há tool habilitada (a `Conversation` normaliza para
# `outro` quando não há) — aqui se testa o que a policy faz com ele.
def test_consulta_responde_com_ferramenta_e_retoma_a_coleta():
    s, acoes = _act(_state(stage=Stage.COLETA_IDADE), _extr(intent=Intent.CONSULTA))

    assert len(acoes) == 1
    assert isinstance(acoes[0], AnswerWithTools)
    assert acoes[0].kind == "answer_with_tools"
    assert "ferramenta" in acoes[0].directive
    assert "pergunte a idade do condutor principal" in acoes[0].directive   # {proxima}
    assert s.stage is Stage.COLETA_IDADE          # a etapa da venda não anda por causa da dúvida
    assert s.ultima_pergunta == "idade"           # mas a pergunta vai junto, e o extractor precisa saber


def test_consulta_aplica_os_campos_da_mesma_mensagem_antes():
    """"quero cotar, tenho 35 anos, e minha apólice está ativa?" — a idade não se perde."""
    s, acoes = _act(_state(stage=Stage.COLETA_IDADE), _extr(intent=Intent.CONSULTA, idade=35))

    assert s.idade == 35
    assert isinstance(acoes[0], AnswerWithTools)
    assert "pergunte qual plano ele quer cotar" in acoes[0].directive   # próxima do roteiro (F8)
    assert s.ultima_pergunta == "plano"


def test_consulta_nao_conta_como_turno_sem_progresso():
    s, _ = _act(_state(stage=Stage.COLETA_IDADE, turnos_sem_progresso=2), _extr(intent=Intent.CONSULTA))
    assert s.turnos_sem_progresso == 0
    assert s.stage is Stage.COLETA_IDADE          # não escalou para humano


def test_consulta_com_pendencia_vira_a_proxima_pergunta():
    """Ano-modelo pendente + dúvida: responde a dúvida e emenda a correção pendente."""
    s, acoes = _act(
        _state(idade=35, plano_id="completo"), _extr(intent=Intent.CONSULTA, veiculo_ano=2027)
    )
    assert isinstance(acoes[0], AnswerWithTools)
    assert "pergunte o modelo e o ano de fabricação do carro" in acoes[0].directive
    assert s.veiculo_ano is None                  # o ano suspeito continua não sendo gravado


def test_consulta_depois_da_cotacao_usa_a_diretiva_pos_cotacao():
    estado = _completo(stage=Stage.APRESENTADO, quote_result=_result())
    s, acoes = _act(estado, _extr(intent=Intent.CONSULTA))
    assert isinstance(acoes[0], AnswerWithTools)
    assert DIRETIVA_POS_COTACAO in acoes[0].directive
    assert s.stage is Stage.APRESENTADO


def test_consulta_em_estado_terminal_continua_encerrada():
    s, acoes = _act(_state(stage=Stage.HANDOFF), _extr(intent=Intent.CONSULTA))
    assert isinstance(acoes[0], SendText)
    assert s.stage is Stage.HANDOFF


def test_fora_de_escopo_continua_indo_para_humano():
    """A consulta não roubou o `fora_de_escopo`: o que nenhuma ferramenta resolve ainda escala."""
    s, acoes = _act(_state(idade=35), _extr(intent=Intent.FORA_DE_ESCOPO))
    assert isinstance(acoes[0], Handoff)
    assert s.handoff_reason is HandoffReason.FORA_DE_ESCOPO


def test_diretiva_de_consulta_vem_do_slot(store_tmp):
    store_tmp.add_version("responder.diretiva_consulta", "v2", "consulte a ferramenta e depois: {proxima}")
    _, acoes = _act(_state(stage=Stage.COLETA_IDADE), _extr(intent=Intent.CONSULTA))
    assert acoes[0].directive == "consulte a ferramenta e depois: pergunte a idade do condutor principal"


# --------------------------------------------------------------------------- plano assumido (F8)
def _esperando_plano(**kw) -> LeadState:
    """Estado logo depois do `AskPlan`: idade coletada, plano pendente."""
    return _state(idade=35, stage=Stage.ESCOLHA_PLANO, ultima_pergunta="plano", **kw)


def test_plano_assumido_quando_o_lead_nao_sabe():
    s, acoes = _act(_esperando_plano(), _extr(intent=Intent.NAO_SEI))
    assert s.plano_id == "essencial"
    assert s.plano_assumido is True
    assert not any(isinstance(a, AskPlan) for a in acoes)     # nunca repete a pergunta
    assert acoes == [AskField(campo="veiculo", motivo=None)]  # segue o roteiro


def test_plano_assumido_quando_o_lead_avanca_outro_campo():
    s, acoes = _act(_esperando_plano(), _extr(veiculo_texto="Onix 2019", veiculo_ano=2019))
    assert s.plano_id == "essencial" and s.plano_assumido is True
    assert acoes == [AskField(campo="cep", motivo=None)]


def test_plano_assumido_conta_como_progresso():
    """Assumir é avançar: um "tanto faz" não pode empurrar a conversa para o humano."""
    s, _ = _act(_esperando_plano(turnos_sem_progresso=2), _extr(intent=Intent.OUTRO))
    assert s.plano_assumido is True
    assert s.turnos_sem_progresso == 0
    assert s.stage is not Stage.HANDOFF


def test_plano_escolhido_depois_desmarca_o_assumido():
    s, _ = _act(_esperando_plano(), _extr(intent=Intent.NAO_SEI))
    s, _ = _act(s, _extr(intent=Intent.ESCOLHER_PLANO, plano_id="premium"))
    assert s.plano_id == "premium" and s.plano_assumido is False


def test_plano_padrao_vem_do_parametro(store_tmp):
    store_tmp.set_overrides("tools", {"policy": {"plano_padrao": "premium"}})
    s, _ = _act(_esperando_plano(), _extr(intent=Intent.NAO_SEI))
    assert s.plano_id == "premium" and s.plano_assumido is True


def test_plano_padrao_invalido_cai_no_primeiro_da_api(store_tmp):
    """Parâmetro digitado errado no Studio não pode virar 422 na cara do lead."""
    store_tmp.set_overrides("tools", {"policy": {"plano_padrao": "nao_existe"}})
    s, _ = _act(_esperando_plano(), _extr(intent=Intent.NAO_SEI))
    assert s.plano_id == "essencial"      # 1º do /planos


def test_pergunta_do_plano_nao_e_assumida_antes_de_ser_feita():
    s, acoes = _act(_state(stage=Stage.COLETA_IDADE), _extr(idade=35))
    assert isinstance(acoes[0], AskPlan)
    assert s.plano_id is None and s.plano_assumido is False


# --------------------------------------------------------------------------- vários carros (F8)
def _carros(*pares) -> Extraction:
    return _extr(veiculos=[VeiculoExtraido(texto=t, ano=a) for t, a in pares])


def test_dois_carros_viram_dois_requests_do_mesmo_plano():
    estado = _state(idade=35, plano_id="completo", cep="01001000", cep_confirmado=True)
    s, acoes = _act(estado, _carros(("Onix 2022", 2022), ("HB20 2020", 2020)))

    assert [(v.texto, v.ano) for v in s.veiculos] == [("Onix 2022", 2022), ("HB20 2020", 2020)]
    assert s.veiculo_texto == "Onix 2022" and s.veiculo_ano == 2022   # espelho do primeiro
    assert isinstance(acoes[0], DoQuotes)
    assert [r.veiculo_ano for r in acoes[0].requests] == [2022, 2020]
    assert {r.plano_id for r in acoes[0].requests} == {"completo"}
    assert {r.idade for r in acoes[0].requests} == {35}
    assert {r.cep for r in acoes[0].requests} == {"01001000"}


def test_carro_sem_ano_segura_a_cotacao_e_diz_qual_e():
    estado = _state(idade=35, plano_id="completo", cep="01001000", cep_confirmado=True)
    s, acoes = _act(estado, _carros(("Onix 2022", 2022), ("HB20", None)))

    assert len(s.veiculos) == 2
    assert isinstance(acoes[0], AskField) and acoes[0].campo == "veiculo"
    assert "HB20" in acoes[0].motivo


def test_ano_informado_depois_completa_o_carro_certo():
    estado = _state(idade=35, plano_id="completo", cep="01001000", cep_confirmado=True)
    s, _ = _act(estado, _carros(("Onix 2022", 2022), ("HB20", None)))
    s, acoes = _act(s, _extr(veiculos=[VeiculoExtraido(texto="HB20", ano=2020)]))

    assert [(v.texto, v.ano) for v in s.veiculos] == [("Onix 2022", 2022), ("HB20", 2020)]
    assert isinstance(acoes[0], DoQuotes) and len(acoes[0].requests) == 2


def test_teto_de_carros_avisa_uma_vez_e_ignora_o_excedente(store_tmp):
    store_tmp.set_overrides("tools", {"policy": {"max_veiculos": 2}})
    estado = _state(idade=35, plano_id="completo", cep="01001000", cep_confirmado=True)
    s, acoes = _act(estado, _carros(("Onix 2022", 2022), ("HB20 2020", 2020), ("Gol 2015", 2015)))

    assert [v.texto for v in s.veiculos] == ["Onix 2022", "HB20 2020"]
    avisos = [a for a in acoes if isinstance(a, SendText)]
    assert len(avisos) == 1 and "2 carros" in avisos[0].text


def test_pergunta_seguinte_avisa_que_sao_varios_carros():
    estado = _state(idade=35, plano_id="completo")
    _, acoes = _act(estado, _carros(("Onix 2022", 2022), ("HB20 2020", 2020)))
    assert isinstance(acoes[0], AskField) and acoes[0].campo == "cep"
    assert "Onix 2022; HB20 2020" in acoes[0].motivo


def test_um_carro_nao_ganha_contexto_de_varios():
    estado = _state(idade=35, plano_id="completo")
    _, acoes = _act(estado, _carros(("Onix 2022", 2022)))
    assert acoes == [AskField(campo="cep", motivo=None)]


# --------------------------------------------------------------------------- pós-cotação com N (F8)
def _cotados(*outcomes) -> LeadState:
    """Estado em COTANDO com um resultado por carro."""
    veiculos = []
    for n, outcome in enumerate(outcomes):
        motivo = "Veiculo com mais de 20 anos nao e aceito." if outcome is QuoteOutcome.RECUSA else None
        veiculos.append(
            VeiculoColetado(
                texto=f"Carro {n}", ano=2019 + n,
                quote_result=_result(outcome=outcome, motivo_recusa=motivo),
            )
        )
    estado = _completo(stage=Stage.COTANDO, veiculos=veiculos)
    estado.quote_result = veiculos[0].quote_result
    return estado


def test_dois_ok_viram_uma_apresentacao_so():
    s, acoes = _act(_cotados(QuoteOutcome.OK, QuoteOutcome.OK), None)
    assert len(acoes) == 1
    assert isinstance(acoes[0], PresentMany)
    assert len(acoes[0].resultados) == 2
    assert s.stage is Stage.APRESENTADO


def test_um_ok_e_um_recusado_ainda_apresenta():
    s, acoes = _act(_cotados(QuoteOutcome.OK, QuoteOutcome.RECUSA), None)
    assert isinstance(acoes[0], PresentMany)
    assert s.stage is Stage.APRESENTADO


def test_um_ok_e_um_indisponivel_ainda_apresenta():
    _, acoes = _act(_cotados(QuoteOutcome.OK, QuoteOutcome.INDISPONIVEL), None)
    assert isinstance(acoes[0], PresentMany)


def test_nenhum_ok_com_indisponivel_vai_para_humano():
    s, acoes = _act(_cotados(QuoteOutcome.RECUSA, QuoteOutcome.INDISPONIVEL), None)
    assert isinstance(acoes[0], Handoff)
    assert s.handoff_reason is HandoffReason.COTACAO_INDISPONIVEL


def test_todos_recusados_encerra_com_recusa():
    s, acoes = _act(_cotados(QuoteOutcome.RECUSA, QuoteOutcome.RECUSA), None)
    assert isinstance(acoes[0], Refuse)
    assert s.stage is Stage.ENCERRADO_RECUSA


def test_um_carro_continua_no_present_de_sempre():
    _, acoes = _act(_cotados(QuoteOutcome.OK), None)
    assert isinstance(acoes[0], Present)


def test_aceite_leva_a_lista_de_cotacoes_para_o_humano():
    estado = _cotados(QuoteOutcome.OK, QuoteOutcome.OK)
    estado.stage = Stage.APRESENTADO
    s, acoes = _act(estado, _extr(intent=Intent.ACEITAR))

    assert isinstance(acoes[0], Handoff)
    payload = acoes[0].payload
    assert [c["carro"] for c in payload["cotacoes"]] == ["Carro 0 2019", "Carro 1 2020"]
    assert payload["cotacao"] is not None                      # o 1º carro continua onde estava
    assert [v["texto"] for v in payload["dados"]["veiculos"]] == ["Carro 0", "Carro 1"]
    assert s.handoff_reason is HandoffReason.LEAD_ACEITOU


def test_troca_de_plano_recota_todos_os_carros():
    estado = _cotados(QuoteOutcome.OK, QuoteOutcome.OK)
    estado.stage = Stage.APRESENTADO
    _, acoes = _act(estado, _extr(intent=Intent.ESCOLHER_PLANO, plano_id="premium"))
    assert isinstance(acoes[0], DoQuotes)
    assert len(acoes[0].requests) == 2
    assert {r.plano_id for r in acoes[0].requests} == {"premium"}

