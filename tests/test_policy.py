"""Testes da máquina de estados. Puros: sem rede, sem LLM, sem docker."""
from __future__ import annotations

from datetime import date

import pytest

from agent.config import settings
from agent.models import (
    AskField,
    AskPlan,
    CepInfo,
    ConfirmCep,
    DoQuote,
    Extraction,
    Handoff,
    HandoffReason,
    Intent,
    LeadState,
    PlanoId,
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
    Stage,
    Violation,
)
from agent.policy import TXT_INSTABILIDADE, next_action

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

    s, acoes = _act(s, _extr(idade=35))
    assert acoes == [AskField(campo="veiculo")]
    assert s.stage is Stage.COLETA_VEICULO

    s, acoes = _act(s, _extr(veiculo_texto="Onix 2019", veiculo_ano=2019))
    assert acoes == [AskField(campo="cep")]
    assert s.veiculo_texto == "Onix 2019"

    s, acoes = _act(s, _extr(cep="01001-000"))
    assert acoes == []  # aguardando o lookup do conversation
    assert s.stage is Stage.CONFIRMA_CEP
    assert s.cep == "01001000"

    s.cep_info = CepInfo(cep="01001000", existe=True, cidade="São Paulo", uf="SP")
    s, acoes = _act(s, None)
    assert acoes == [ConfirmCep(cep="01001000", cidade="São Paulo", uf="SP")]

    s, acoes = _act(s, _extr(intent=Intent.CONFIRMAR))
    assert isinstance(acoes[0], AskPlan)
    assert len(acoes[0].planos) == 3
    assert s.stage is Stage.ESCOLHA_PLANO

    s, acoes = _act(s, _extr(intent=Intent.ESCOLHER_PLANO, plano_id="completo"))
    assert acoes == [
        DoQuote(
            request=QuoteRequest(
                plano_id="completo", idade=35, veiculo_ano=2019, cep="01001000",
                data_inicio="2026-09-01",
            )
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
    assert isinstance(acoes[0], DoQuote)
    assert acoes[0].request.cep is None


# --------------------------------------------------------------------------- data de início
def test_data_no_passado_avisa_e_nao_grava():
    s, acoes = _act(_state(idade=35), _extr(data_inicio=date(2026, 1, 1)))
    assert isinstance(acoes[0], SendText)
    assert s.data_inicio is None


def test_data_futura_vai_para_a_cotacao():
    s, acoes = _act(_completo(plano_id=None), _extr(plano_id="completo", data_inicio=date(2026, 10, 1)))
    assert isinstance(acoes[0], DoQuote)
    assert acoes[0].request.data_inicio == "2026-10-01"
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
    assert isinstance(acoes[0], DoQuote)
    assert acoes[0].request.plano_id == novo_plano
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
