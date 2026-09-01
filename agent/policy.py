"""Máquina de estados do agente: a ÚNICA camada que decide.

Pura por construção — recebe estado + extração e devolve um NOVO estado e uma
lista de ações. Sem I/O e sem LLM, porque decisão de venda (cotar, recusar,
escalar) precisa ser determinística, testável e auditável; o LLM só extrai
dados e transforma `AskField`/`Reply` em texto.

Duas re-entradas do `conversation.py` chegam com `extraction=None` e NÃO são
mensagem de mídia: pós-cotação (stage COTANDO com `quote_result`) e pós-lookup
de CEP (stage CONFIRMA_CEP com `cep_info`). Elas são tratadas antes da regra
de mídia justamente para não confundir "não veio texto" com "voltei do I/O".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

from agent.config import settings
from agent.models import (
    Action,
    AskField,
    AskPlan,
    CampoColeta,
    ConfirmCep,
    DoQuote,
    Extraction,
    Handoff,
    HandoffReason,
    Intent,
    LeadState,
    Present,
    QuoteOutcome,
    QuoteRequest,
    Refuse,
    Reply,
    SendText,
    Stage,
)

if TYPE_CHECKING:  # pragma: no cover - só para type hints; o executor A entrega rules.py
    from agent.rules import Rules

# --------------------------------------------------------------------------- textos fixos
TXT_MIDIA = "Não consigo ouvir áudio/abrir arquivos por aqui. Pode me escrever?"
TXT_DESPEDIDA = (
    "Tudo bem, sem problema! Se mudar de ideia é só me chamar por aqui. Obrigado pelo contato."
)
TXT_TERMINAL_HANDOFF = (
    "Um consultor já está com o seu caso e responde por aqui mesmo. "
    "Pode deixar a mensagem que ele vê."
)
TXT_TERMINAL_ENCERRADO = (
    "Esse atendimento já foi encerrado por aqui. Se quiser retomar, é só chamar "
    "que um consultor te ajuda."
)
TXT_AGUARDE = "Só um instante, estou puxando a cotação certinho pra você."
TXT_DATA_PASSADA = (
    "Essa data de início já passou, então não consigo usar. A vigência começa a "
    "partir de hoje — se quiser começar depois, me diga outra data."
)

DIRETIVA_OBJECAO = (
    "lead achou caro; ofereça ver outro plano (mais franquia = parcela menor); "
    "NÃO dê desconto nem cite valores novos"
)
DIRETIVA_POS_COTACAO = (
    "lead respondeu depois da cotação sem aceitar nem recusar; retome a dúvida dele "
    "e pergunte se quer fechar ou ver outro plano; NÃO cite valores novos"
)
DIRETIVA_MESMO_PLANO = (
    "lead repetiu o plano que já está cotado; confirme se quer fechar esse mesmo "
    "ou ver outro; NÃO cite valores novos"
)
MOTIVO_ANO_MODELO = "ano futuro parece ano-modelo; confirmar ano de fabricação"

STAGES_TERMINAIS = frozenset({Stage.HANDOFF, Stage.ENCERRADO, Stage.ENCERRADO_RECUSA})

# Intents que, mesmo sem trazer dado novo, movem a conversa (não contam como estagnação).
INTENTS_UTEIS = frozenset(
    {
        Intent.CONFIRMAR,
        Intent.NEGAR,
        Intent.NAO_SEI,
        Intent.ACEITAR,
        Intent.ESCOLHER_PLANO,
        Intent.OBJECAO_PRECO,
        Intent.PEDIR_DESCONTO,
    }
)

_STAGE_DO_CAMPO: dict[CampoColeta, Stage] = {
    "idade": Stage.COLETA_IDADE,
    "veiculo": Stage.COLETA_VEICULO,
    "cep": Stage.COLETA_CEP,
    "plano": Stage.ESCOLHA_PLANO,
    "data_inicio": Stage.ESCOLHA_PLANO,
}


@dataclass
class _Absorcao:
    """Resultado de absorver os dados da mensagem atual no estado."""

    avisos: list[Action] = field(default_factory=list)      # informativos, não bloqueiam
    pendencias: list[Action] = field(default_factory=list)  # correções, bloqueiam o fluxo
    refuse: Refuse | None = None                            # recusa de negócio, encerra
    campos_alterados: bool = False


# --------------------------------------------------------------------------- entrada
def next_action(
    state: LeadState,
    extraction: Extraction | None,
    rules: Rules,
    today: date,
) -> tuple[LeadState, list[Action]]:
    """Decide o próximo passo. Não muta `state`: devolve uma cópia."""
    s = state.model_copy(deep=True)

    # Re-entradas do conversation (não são turno novo do lead).
    if extraction is None:
        if s.stage is Stage.COTANDO and s.quote_result is not None:
            return _pos_cotacao(s)
        if s.stage is Stage.CONFIRMA_CEP and s.cep_info is not None and not s.cep_confirmado:
            acoes = _resolver_cep(s)
            return (s, acoes) if acoes is not None else (s, _fluxo(s, rules, today))

    s.turnos += 1

    # Estado terminal: responde educado, sem reabrir a coleta.
    if s.stage in STAGES_TERMINAIS:
        texto = TXT_TERMINAL_HANDOFF if s.stage is Stage.HANDOFF else TXT_TERMINAL_ENCERRADO
        return s, [SendText(text=texto)]

    if extraction is None:
        return _com_estagnacao(s, [SendText(text=TXT_MIDIA)], progresso=False)

    intent = extraction.intent
    if intent is Intent.PEDIR_HUMANO:
        return _handoff(s, HandoffReason.LEAD_PEDIU_HUMANO)
    if intent is Intent.FORA_DE_ESCOPO:
        return _handoff(s, HandoffReason.FORA_DE_ESCOPO)
    if intent is Intent.RECUSAR:
        s.stage = Stage.ENCERRADO
        return s, [SendText(text=TXT_DESPEDIDA)]

    abs_ = _absorver(s, extraction, rules, today)
    if abs_.refuse is not None:
        s.stage = Stage.ENCERRADO_RECUSA
        return s, [abs_.refuse]

    progresso = abs_.campos_alterados or intent in INTENTS_UTEIS
    s, escalou = _atualizar_estagnacao(s, progresso)
    if escalou is not None:
        return s, escalou

    if abs_.pendencias:
        return s, abs_.avisos + abs_.pendencias

    # CEP recém-informado ou ainda não confirmado.
    if s.stage is Stage.CONFIRMA_CEP and not s.cep_confirmado:
        acoes = _resolver_cep(s)
        if acoes is not None:
            return s, abs_.avisos + acoes

    if s.stage is Stage.APRESENTADO:
        return _pos_apresentacao(s, extraction, abs_, rules, today)

    # Mensagem que chegou enquanto a cotação está em voo e não mudou nada.
    if s.stage is Stage.COTANDO and s.quote_result is None and not abs_.campos_alterados:
        return s, abs_.avisos + [SendText(text=TXT_AGUARDE)]

    return s, abs_.avisos + _fluxo(s, rules, today)


# --------------------------------------------------------------------------- absorção
def _absorver(s: LeadState, e: Extraction, rules: Rules, today: date) -> _Absorcao:
    """Grava no estado qualquer campo citado, em qualquer estágio (lead não segue roteiro)."""
    out = _Absorcao()

    if e.idade is not None:
        violacao = rules.validate_idade(e.idade)
        if violacao is not None:
            out.refuse = Refuse(motivo=violacao.motivo)
            return out
        out.campos_alterados = out.campos_alterados or s.idade != e.idade
        s.idade = e.idade

    if e.veiculo_ano is not None:
        if e.ano_parece_modelo or e.veiculo_ano > today.year:
            # Não grava: 2027 quase sempre é ano-modelo, e ano errado vira preço errado.
            s.stage = Stage.COLETA_VEICULO
            s.ultima_pergunta = "veiculo"
            out.pendencias.append(AskField(campo="veiculo", motivo=MOTIVO_ANO_MODELO))
        else:
            violacao = rules.validate_veiculo_ano(e.veiculo_ano)
            if violacao is not None:
                out.refuse = Refuse(motivo=violacao.motivo)
                return out
            out.campos_alterados = out.campos_alterados or s.veiculo_ano != e.veiculo_ano
            s.veiculo_ano = e.veiculo_ano
            if e.veiculo_texto:
                s.veiculo_texto = e.veiculo_texto
    elif e.veiculo_texto:
        out.campos_alterados = out.campos_alterados or s.veiculo_texto != e.veiculo_texto
        s.veiculo_texto = e.veiculo_texto

    if e.cep is not None:
        cep8 = rules.normalize_cep(e.cep)
        if cep8 is None:
            s.cep_tentativas += 1
            if s.cep_tentativas > settings.max_cep_tentativas:
                # Insistir mais só irrita: cota sem CEP e avisa que o valor pode subir.
                s.cep_ausente = True
                out.campos_alterados = True
            else:
                s.stage = Stage.COLETA_CEP
                s.ultima_pergunta = "cep"
                out.pendencias.append(AskField(campo="cep", motivo="formato inválido"))
        else:
            out.campos_alterados = True
            s.cep = cep8
            s.cep_info = None
            s.cep_confirmado = False
            s.cep_ausente = False
            s.stage = Stage.CONFIRMA_CEP
    elif e.intent is Intent.CONFIRMAR and s.stage is Stage.CONFIRMA_CEP:
        s.cep_confirmado = True
        out.campos_alterados = True
    elif e.intent is Intent.NEGAR and s.stage is Stage.CONFIRMA_CEP:
        s.cep_tentativas += 1
        s.cep = None
        s.cep_info = None
        s.cep_confirmado = False
        out.campos_alterados = True
        if s.cep_tentativas > settings.max_cep_tentativas:
            s.cep_ausente = True
        else:
            s.stage = Stage.COLETA_CEP
            s.ultima_pergunta = "cep"
            out.pendencias.append(
                AskField(campo="cep", motivo="lead disse que o CEP está errado; pedir de novo")
            )
    elif e.intent is Intent.NAO_SEI and (
        s.stage is Stage.COLETA_CEP or s.ultima_pergunta == "cep"
    ):
        s.cep_ausente = True
        out.campos_alterados = True

    if e.plano_id is not None:
        out.campos_alterados = out.campos_alterados or s.plano_id != e.plano_id
        s.plano_id = e.plano_id

    if e.data_inicio is not None and not e.data_vaga:
        violacao = rules.validate_data_inicio(e.data_inicio)
        if violacao is not None:
            out.avisos.append(SendText(text=TXT_DATA_PASSADA))
        else:
            out.campos_alterados = out.campos_alterados or s.data_inicio != e.data_inicio
            s.data_inicio = e.data_inicio

    return out


# --------------------------------------------------------------------------- CEP
def _resolver_cep(s: LeadState) -> list[Action] | None:
    """Ações do estágio CONFIRMA_CEP. `None` = CEP resolvido, pode seguir o fluxo."""
    info = s.cep_info
    if info is None:
        return []  # o conversation ainda vai consultar o ViaCEP e chamar de novo
    if info.existe is None:
        # ViaCEP fora do ar não pode travar a venda: aceita o que o lead informou.
        s.cep_confirmado = True
        return None
    if info.existe:
        return [ConfirmCep(cep=s.cep or info.cep, cidade=info.cidade or "", uf=info.uf or "")]

    s.cep_tentativas += 1
    if s.cep_tentativas > settings.max_cep_tentativas:
        s.cep_confirmado = True  # segue com o CEP como está
        return None
    s.stage = Stage.COLETA_CEP
    s.ultima_pergunta = "cep"
    return [AskField(campo="cep", motivo="não encontrei esse CEP; pedir de novo")]


# --------------------------------------------------------------------------- fluxo de coleta
def _campo_faltante(s: LeadState) -> CampoColeta | None:
    """Ordem de coleta: idade → veículo → CEP → plano."""
    if s.idade is None:
        return "idade"
    if s.veiculo_ano is None:
        return "veiculo"
    if s.cep is None and not s.cep_ausente:
        return "cep"
    if s.plano_id is None:
        return "plano"
    return None


def _fluxo(s: LeadState, rules: Rules, today: date) -> list[Action]:
    """Pergunta o próximo campo faltante ou dispara a cotação."""
    campo = _campo_faltante(s)
    if campo is not None:
        s.stage = _STAGE_DO_CAMPO[campo]
        s.ultima_pergunta = campo
        if campo == "plano":
            return [AskPlan(planos=rules.planos_resumo())]
        return [AskField(campo=campo)]

    s.stage = Stage.COTANDO
    s.quote_result = None  # cotação nova: o resultado antigo não vale mais
    request = QuoteRequest(
        plano_id=s.plano_id,
        idade=s.idade,
        veiculo_ano=s.veiculo_ano,
        cep=None if s.cep_ausente else s.cep,
        data_inicio=(s.data_inicio or today).isoformat(),
    )
    return [DoQuote(request=request)]


# --------------------------------------------------------------------------- pós-cotação
def _pos_cotacao(s: LeadState) -> tuple[LeadState, list[Action]]:
    """Traduz o resultado da API em ação. Preço só aparece aqui, vindo do `Quote`."""
    resultado = s.quote_result
    if resultado is None:  # defensivo: só chamado com o resultado já gravado
        return _handoff(s, HandoffReason.ERRO_INTERNO)
    if resultado.outcome is QuoteOutcome.OK:
        s.stage = Stage.APRESENTADO
        return s, [Present(result=resultado, cep_ausente=s.cep_ausente)]
    if resultado.outcome is QuoteOutcome.RECUSA:
        s.stage = Stage.ENCERRADO_RECUSA
        return s, [Refuse(motivo=resultado.motivo_recusa or "perfil fora dos nossos critérios")]
    if resultado.outcome is QuoteOutcome.BUG:
        return _handoff(s, HandoffReason.ERRO_INTERNO)
    return _handoff(s, HandoffReason.COTACAO_INDISPONIVEL)


def _pos_apresentacao(
    s: LeadState,
    e: Extraction,
    abs_: _Absorcao,
    rules: Rules,
    today: date,
) -> tuple[LeadState, list[Action]]:
    """Depois da cotação na mesa: fechar, trocar de plano, ou objeção de preço."""
    if e.intent is Intent.ACEITAR:
        return _handoff(s, HandoffReason.LEAD_ACEITOU)

    if e.intent is Intent.PEDIR_DESCONTO:
        return _handoff(s, HandoffReason.NEGOCIACAO)

    if e.intent is Intent.OBJECAO_PRECO:
        s.objecoes += 1
        if s.objecoes >= 2 or _pede_desconto(e):
            return _handoff(s, HandoffReason.NEGOCIACAO)
        return s, [Reply(directive=DIRETIVA_OBJECAO)]

    if abs_.campos_alterados:
        return s, abs_.avisos + _fluxo(s, rules, today)

    if e.intent is Intent.ESCOLHER_PLANO:
        return s, abs_.avisos + [Reply(directive=DIRETIVA_MESMO_PLANO)]
    return s, abs_.avisos + [Reply(directive=DIRETIVA_POS_COTACAO)]


def _pede_desconto(e: Extraction) -> bool:
    """Desconto é decisão comercial: não é do agente, vai direto pro humano."""
    return bool(e.observacao) and "desconto" in e.observacao.lower()


# --------------------------------------------------------------------------- handoff / estagnação
def _dados_coletados(s: LeadState) -> dict[str, Any]:
    return {
        "nome": s.lead_nome,
        "idade": s.idade,
        "veiculo_texto": s.veiculo_texto,
        "veiculo_ano": s.veiculo_ano,
        "cep": s.cep,
        "cep_cidade": s.cep_info.cidade if s.cep_info else None,
        "cep_uf": s.cep_info.uf if s.cep_info else None,
        "cep_ausente": s.cep_ausente,
        "plano_id": s.plano_id,
        "data_inicio": s.data_inicio.isoformat() if s.data_inicio else None,
    }


def _payload_handoff(s: LeadState, reason: HandoffReason) -> dict[str, Any]:
    """O humano precisa receber tudo pronto: dados, cotação (se houver) e o porquê."""
    payload: dict[str, Any] = {
        "dados": _dados_coletados(s),
        "cotacao": None,
        "motivo": reason.value,
        "conversation_id": s.conversation_id,
    }
    if s.quote_result is not None:
        payload["cotacao"] = s.quote_result.model_dump(mode="json")
        payload["quote_id"] = s.quote_result.quote_id
    return payload


def _handoff(s: LeadState, reason: HandoffReason) -> tuple[LeadState, list[Action]]:
    s.stage = Stage.HANDOFF
    s.handoff_reason = reason
    return s, [Handoff(reason=reason, payload=_payload_handoff(s, reason))]


def _atualizar_estagnacao(
    s: LeadState, progresso: bool
) -> tuple[LeadState, list[Action] | None]:
    """Conta turnos que não avançam a coleta; no limite, chama humano em vez de insistir."""
    if progresso:
        s.turnos_sem_progresso = 0
        return s, None
    s.turnos_sem_progresso += 1
    if s.turnos_sem_progresso >= settings.max_turnos_sem_progresso:
        s, acoes = _handoff(s, HandoffReason.SEM_PROGRESSO)
        return s, acoes
    return s, None


def _com_estagnacao(
    s: LeadState, acoes: list[Action], progresso: bool
) -> tuple[LeadState, list[Action]]:
    s, escalou = _atualizar_estagnacao(s, progresso)
    return (s, escalou) if escalou is not None else (s, acoes)
