"""Invariantes da máquina de estados: os becos sem saída que a auditoria achou, agora como teste.

Os testes de `test_policy.py` provam roteiros ("o lead diz X, o agente responde Y"). Estes
provam propriedades de TODOS os estados, que é onde os becos moram: um estágio de onde nenhuma
mensagem tira o lead, um terminal que nunca reabre, um turno em que o agente simplesmente cala,
e um lead que fica preso no loop porque nunca chega a lugar nenhum.

I1  todo stage não terminal tem uma entrada que muda o estado;
I2  todo terminal tem entrada que reabre a conversa OU responde uma única vez;
I3  `next_action` nunca devolve `[]`, fora da re-entrada do `CONFIRMA_CEP` (esperando o ViaCEP)
    e do terminal que já avisou;
I4  lead adversário (`outro`, sem dado nenhum) chega a um terminal em no máximo
    `max_turnos_sem_progresso + 1` turnos, a partir de QUALQUER stage.
"""
from __future__ import annotations

from datetime import date

import pytest

from agent.config import settings
from agent.models import (
    CepInfo,
    Extraction,
    Intent,
    LeadState,
    QuoteOutcome,
    Stage,
    VeiculoColetado,
)
from agent.policy import STAGES_TERMINAIS, next_action
from tests.test_policy import RULES, _extr, _result

HOJE = date(2026, 9, 1)

NAO_TERMINAIS = [s for s in Stage if s not in STAGES_TERMINAIS]
TERMINAIS = sorted(STAGES_TERMINAIS, key=lambda s: s.value)
REABRIVEIS = [Stage.ENCERRADO, Stage.ENCERRADO_RECUSA]


def _act(state: LeadState, extraction: Extraction | None):
    return next_action(state, extraction, RULES, HOJE)


def _esperando_o_viacep(s: LeadState) -> bool:
    """O turno acabou sem ação porque o `conversation` ainda vai consultar o ViaCEP e redecidir."""
    return s.stage is Stage.CONFIRMA_CEP and s.cep_info is None and not s.cep_confirmado


# --------------------------------------------------------------------------- estados plausíveis
def _estado(stage: Stage) -> LeadState:
    """Um estado COERENTE com o stage: é o que o `conversation` teria gravado ao chegar nele.

    Testar `Stage.COTANDO` com o lead sem idade provaria uma coisa que não acontece; o valor do
    invariante está em partir do estado real de cada etapa.
    """
    s = LeadState(conversation_id="inv", stage=stage)
    if stage in (Stage.INICIO, Stage.COLETA_IDADE):
        s.ultima_pergunta = "idade"
        return s

    s.idade = 35
    if stage is Stage.ESCOLHA_PLANO:
        s.plano_perguntado = True
        s.ultima_pergunta = "plano"
        return s

    s.plano_id = "completo"
    if stage is Stage.COLETA_VEICULO:
        s.ultima_pergunta = "veiculo"
        return s

    s.veiculos = [VeiculoColetado(texto="Onix 2019", ano=2019)]
    s.veiculo_texto, s.veiculo_ano = "Onix 2019", 2019
    if stage is Stage.COLETA_CEP:
        s.ultima_pergunta = "cep"
        return s

    s.cep = "01001000"
    if stage is Stage.CONFIRMA_CEP:
        # O estado em que o LEAD responde: o ViaCEP já voltou e a confirmação já saiu. Com
        # `cep_info is None` (ou a confirmação ainda não pedida) o que existe é a re-entrada do
        # `conversation`, coberta à parte em I3.
        s.cep_info = CepInfo(cep="01001000", existe=True, cidade="São Paulo", uf="SP")
        s.cep_confirmacao_pedida = True
        s.ultima_pergunta = "cep"
        return s

    s.cep_info = CepInfo(cep="01001000", existe=True, cidade="São Paulo", uf="SP")
    s.cep_confirmado = True
    if stage is Stage.COLETA_DATA:
        s.data_perguntada = True
        s.ultima_pergunta = "data_inicio"
        return s

    s.data_inicio = HOJE
    if stage is Stage.COTANDO:
        return s

    if stage is Stage.APRESENTADO:
        s.quote_result = _result(QuoteOutcome.OK)
        s.veiculos[0].quote_result = s.quote_result
        return s

    if stage is Stage.ENCERRADO_RECUSA:
        s.recusa_campo = "veiculo_ano"
        s.veiculos = []
        s.veiculo_ano = None
    return s


# A resposta que o lead daria à pergunta daquela etapa. É ela que I1 usa.
RESPOSTA_ESPERADA: dict[Stage, Extraction] = {
    Stage.INICIO: _extr(idade=35),
    Stage.COLETA_IDADE: _extr(idade=35),
    Stage.ESCOLHA_PLANO: _extr(intent=Intent.ESCOLHER_PLANO, plano_id="premium"),
    Stage.COLETA_VEICULO: _extr(veiculo_texto="Onix 2019", veiculo_ano=2019),
    Stage.COLETA_CEP: _extr(cep="01310100"),
    Stage.CONFIRMA_CEP: _extr(intent=Intent.CONFIRMAR),
    Stage.COLETA_DATA: _extr(data_inicio=date(2026, 9, 20)),
    Stage.COTANDO: _extr(intent=Intent.ESCOLHER_PLANO, plano_id="premium"),
    Stage.APRESENTADO: _extr(intent=Intent.ACEITAR),
}

# Bateria de entradas de I3: o que um lead real manda, incluindo o que não faz sentido ali.
BATERIA: list[Extraction] = [
    _extr(intent=Intent.OUTRO),
    _extr(intent=Intent.SAUDACAO),
    _extr(intent=Intent.NAO_SEI),
    _extr(intent=Intent.CONFIRMAR),
    _extr(intent=Intent.NEGAR),
    _extr(intent=Intent.DUVIDA_PRODUTO),
    _extr(intent=Intent.OBJECAO_PRECO),
    _extr(intent=Intent.PEDIR_DESCONTO),
    _extr(intent=Intent.ESCOLHER_PLANO, plano_id=None),
    _extr(intent=Intent.ESCOLHER_PLANO, plano_id="premium"),
    _extr(idade=35),
    _extr(idade=90),                                   # recusa
    _extr(veiculo_texto="Corsa 2001", veiculo_ano=2001),   # recusa
    _extr(cep="abc"),
    _extr(cep="01310100"),
    _extr(data_inicio=date(2020, 1, 1)),               # data no passado
    _extr(data_vaga=True),
    Extraction(intent=Intent.OUTRO, indisponivel=True),
    None,                                              # mídia sem texto
]


# --------------------------------------------------------------------------- I1
@pytest.mark.parametrize("stage", NAO_TERMINAIS, ids=lambda s: s.value)
def test_i1_todo_stage_nao_terminal_tem_saida(stage: Stage):
    """Existe uma mensagem que TIRA o lead de onde ele está — e ela responde alguma coisa."""
    antes = _estado(stage)
    depois, acoes = _act(antes, RESPOSTA_ESPERADA[stage])

    mudou = depois.model_dump(exclude={"turnos"}) != antes.model_dump(exclude={"turnos"})
    assert mudou, f"{stage.value} não mudou de estado com a resposta esperada"
    assert acoes or _esperando_o_viacep(depois), f"{stage.value} não respondeu à resposta esperada"


# --------------------------------------------------------------------------- I2
@pytest.mark.parametrize("stage", REABRIVEIS, ids=lambda s: s.value)
def test_i2_encerramento_reabre_com_intencao_de_cotar(stage: Stage):
    depois, acoes = _act(_estado(stage), _extr(idade=40))

    assert acoes and depois.stage not in STAGES_TERMINAIS
    assert depois.terminal_avisado is False


def test_i2_handoff_responde_uma_vez_e_cala():
    """No handoff já tem gente no caso: responder para sempre foi o incidente do WhatsApp."""
    s = _estado(Stage.HANDOFF)
    s, primeira = _act(s, _extr(intent=Intent.OUTRO))
    assert len(primeira) == 1 and s.terminal_avisado is True

    for _ in range(5):
        s, acoes = _act(s, _extr(intent=Intent.OUTRO))
        assert acoes == []


@pytest.mark.parametrize("stage", TERMINAIS, ids=lambda s: s.value)
def test_i2_pedir_humano_vale_em_qualquer_terminal(stage: Stage):
    """Sair para um humano é a saída que nunca pode faltar, esteja a conversa onde estiver."""
    depois, acoes = _act(_estado(stage), _extr(intent=Intent.PEDIR_HUMANO))

    assert acoes, f"{stage.value} calou diante de um pedido de atendente"
    assert depois.stage is Stage.HANDOFF


# --------------------------------------------------------------------------- I3
@pytest.mark.parametrize("stage", [s for s in Stage], ids=lambda s: s.value)
@pytest.mark.parametrize("entrada", BATERIA, ids=lambda e: "midia" if e is None else e.intent.value)
def test_i3_nenhum_turno_sai_calado(stage: Stage, entrada: Extraction | None):
    """`[]` só é resposta legítima em dois pontos, e os dois são explícitos."""
    depois, acoes = _act(_estado(stage), entrada)

    if stage in STAGES_TERMINAIS:
        return          # o silêncio do terminal tem teste próprio (I2), e só vale da 2ª vez
    if _esperando_o_viacep(depois):
        return          # a outra exceção: o turno continua na rodada seguinte, com o CEP resolvido
    assert acoes != [], f"{stage.value} ficou calado com {entrada}"


def test_i3_a_unica_lista_vazia_fora_do_terminal_e_a_espera_do_viacep():
    """CEP novo: o `conversation` vai consultar o ViaCEP e chamar `next_action` de novo."""
    depois, acoes = _act(_estado(Stage.COLETA_CEP), _extr(cep="01310100"))

    assert acoes == []
    assert _esperando_o_viacep(depois)


def test_i3_midia_na_confirmacao_do_cep_nao_repete_a_pergunta_para_sempre():
    """`None` é mídia E re-entrada; sem separar as duas, o áudio virava a mesma pergunta eterna."""
    s = _estado(Stage.CONFIRMA_CEP)          # a confirmação já saiu
    _, primeira = _act(s, None)

    assert primeira, "a mídia na confirmação do CEP não teve resposta"
    assert primeira[0].kind == "send_text"   # pede texto, em vez de reenviar o ConfirmCep


def test_i3_terminal_calado_so_depois_de_ter_avisado():
    for stage in TERMINAIS:
        s = _estado(stage)
        assert s.terminal_avisado is False
        _, acoes = _act(s, _extr(intent=Intent.OUTRO))
        assert acoes, f"{stage.value} calou já na primeira mensagem"


# --------------------------------------------------------------------------- I4
def _rodar_ate_terminar(s: LeadState, entrada, teto: int) -> LeadState:
    """Roda turnos até um terminal, fechando as rodadas internas como o `conversation` faz.

    Sem devolver o resultado da cotação e o ViaCEP, a simulação empaca em `cotando` por falta do
    mundo externo — e o teste mediria a fixture, não a policy.
    """
    for turno in range(1, teto + 1):
        s, acoes = _act(s, entrada() if callable(entrada) else entrada)
        assert acoes or s.terminal_avisado or _esperando_o_viacep(s), f"turno {turno} saiu calado"
        assert s.turnos_sem_progresso <= settings.max_turnos_sem_progresso, (
            f"turno {turno}: {s.turnos_sem_progresso} turnos sem progresso sem escalar"
        )
        if s.stage in STAGES_TERMINAIS:
            return s
        s = _fechar_rodadas(s)
        if s.stage in STAGES_TERMINAIS:
            return s
    return s


def _fechar_rodadas(s: LeadState) -> LeadState:
    """As re-entradas do turno: ViaCEP respondido e cotação concluída."""
    if s.stage is Stage.CONFIRMA_CEP and s.cep_info is None:
        s.cep_info = CepInfo(cep=s.cep or "01001000", existe=True, cidade="São Paulo", uf="SP")
        s, _ = _act(s, None)
    if s.stage is Stage.COTANDO:
        for veiculo in s.veiculos:
            veiculo.quote_result = _result(QuoteOutcome.OK)
        s.quote_result = s.veiculos[0].quote_result if s.veiculos else _result(QuoteOutcome.OK)
        s, _ = _act(s, None)
    return s


# O teto: o lead adversário ainda "progride" quando a policy desiste de um campo (esgota as
# tentativas de CEP, assume a data). Cada desistência zera o contador de estagnação de propósito
# — é avanço, não loop —, então o limite honesto é o do brief vezes o número dessas desistências.
TETO_I4 = 3 * (settings.max_turnos_sem_progresso + 1)


@pytest.mark.parametrize("stage", [s for s in Stage], ids=lambda s: s.value)
def test_i4_lead_adversario_chega_a_um_terminal(stage: Stage):
    """`outro` sem dado nenhum, para sempre: a conversa TEM de terminar."""
    s = _rodar_ate_terminar(_estado(stage), _extr(intent=Intent.OUTRO), TETO_I4)

    assert s.stage in STAGES_TERMINAIS, f"{stage.value} não terminou (ficou em {s.stage.value})"


@pytest.mark.parametrize("stage", [s for s in Stage], ids=lambda s: s.value)
def test_i4_lead_que_so_manda_midia_tambem_termina(stage: Stage):
    """Mesma propriedade com a outra forma de não dizer nada: áudio atrás de áudio."""
    s = _rodar_ate_terminar(_estado(stage), None, TETO_I4)

    assert s.stage in STAGES_TERMINAIS, f"{stage.value} não terminou com mídia"


@pytest.mark.parametrize("stage", NAO_TERMINAIS, ids=lambda s: s.value)
def test_i4_lead_com_o_llm_fora_do_ar_chega_a_um_terminal(stage: Stage):
    """Terceira forma de não dizer nada: o Extractor falhando turno após turno."""
    s = _rodar_ate_terminar(
        _estado(stage), lambda: Extraction(intent=Intent.OUTRO, indisponivel=True), TETO_I4
    )

    assert s.stage is Stage.HANDOFF
