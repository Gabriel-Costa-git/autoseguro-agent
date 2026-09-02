"""Máquina de estados do agente: a ÚNICA camada que decide.

Pura por construção — recebe estado + extração e devolve um NOVO estado e uma
lista de ações. Sem I/O e sem LLM, porque decisão de venda (cotar, recusar,
escalar) precisa ser determinística, testável e auditável; o LLM só extrai
dados e transforma `AskField`/`Reply` em texto.

`Intent.CONSULTA` só chega aqui quando existe tool habilitada no painel — a
`Conversation` normaliza para `outro` quando não existe. A policy então devolve
`AnswerWithTools`: responde a pergunta com a ferramenta e retoma a coleta de onde
parou, sem mexer no estágio.

Quando a extração vem marcada `indisponivel` (LLM fora do ar), a policy não
decide nada: pede a mensagem de novo. Repetir a última pergunta seria pior — o
lead veria o mesmo texto várias vezes sem entender por quê.

Duas re-entradas do `conversation.py` chegam com `extraction=None` e NÃO são
mensagem de mídia: pós-cotação (stage COTANDO com `quote_result`) e pós-lookup
de CEP (stage CONFIRMA_CEP com `cep_info`). Elas são tratadas antes da regra
de mídia justamente para não confundir "não veio texto" com "voltei do I/O".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

from agent.defaults import SLOTS
from agent.models import (
    Action,
    AnswerWithTools,
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
from agent.runtime_config import store

if TYPE_CHECKING:  # pragma: no cover - só para type hints; o executor A entrega rules.py
    from agent.rules import Rules

# --------------------------------------------------------------------------- textos (slots do Studio)
def _t(key: str) -> str:
    """Texto ativo do slot, lido NA CHAMADA (o Studio troca a versão em tempo real)."""
    return store.text(key)


def _t_ctx(key: str, **ctx: Any) -> str:
    """Idem, para slot com placeholder."""
    return store.text(key, **ctx)


def _p(nome: str) -> Any:
    """Parâmetro efetivo de `tools.policy.*`/`tools.rules.*` (override > .env > default)."""
    return store.param(nome)


def _default(key: str) -> str:
    """Texto entregue do slot. Só para os símbolos de compatibilidade abaixo."""
    return SLOTS[key]["default"]


# Constantes de compatibilidade: valem o texto ENTREGUE (versão `default`), não o ativo.
# Existem porque `channels/cli.py` e `tests/golden_cases.py` importam esses nomes; o valor
# efetivo, editável no Studio, vem sempre de `_t(...)` dentro das funções.
TXT_MIDIA = _default("policy.txt_midia")
TXT_DESPEDIDA = _default("policy.txt_despedida")
TXT_TERMINAL_HANDOFF = _default("policy.txt_terminal_handoff")
TXT_TERMINAL_ENCERRADO = _default("policy.txt_terminal_encerrado")
TXT_AGUARDE = _default("policy.txt_aguarde")
TXT_INSTABILIDADE = _default("policy.txt_instabilidade")
TXT_DATA_PASSADA = _default("policy.txt_data_passada")
DIRETIVA_OBJECAO = _default("policy.diretiva_objecao")
DIRETIVA_POS_COTACAO = _default("policy.diretiva_pos_cotacao")
DIRETIVA_MESMO_PLANO = _default("policy.diretiva_mesmo_plano")
MOTIVO_ANO_MODELO = _default("policy.motivo_ano_modelo")

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
        Intent.CONSULTA,
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
        terminal = "handoff" if s.stage is Stage.HANDOFF else "encerrado"
        texto = _t(f"policy.txt_terminal_{terminal}")
        return s, [SendText(text=texto)]

    if extraction is None:
        return _com_estagnacao(s, [SendText(text=_t("policy.txt_midia"))], progresso=False)

    # Extração indisponível (LLM fora): não sabemos o que o lead disse, então NÃO
    # re-executamos o fluxo — foi assim que o lead levou a lista de planos 3x seguidas.
    # Pede para repetir e conta como turno sem progresso: se o LLM ficar fora, escala.
    if extraction.indisponivel:
        return _com_estagnacao(s, [SendText(text=_t("policy.txt_instabilidade"))], progresso=False)

    intent = extraction.intent
    if intent is Intent.PEDIR_HUMANO:
        return _handoff(s, HandoffReason.LEAD_PEDIU_HUMANO)
    if intent is Intent.FORA_DE_ESCOPO:
        return _handoff(s, HandoffReason.FORA_DE_ESCOPO)
    if intent is Intent.RECUSAR:
        s.stage = Stage.ENCERRADO
        return s, [SendText(text=_t("policy.txt_despedida"))]

    abs_ = _absorver(s, extraction, rules, today)
    if abs_.refuse is not None:
        s.stage = Stage.ENCERRADO_RECUSA
        return s, [abs_.refuse]

    progresso = abs_.campos_alterados or intent in INTENTS_UTEIS
    s, escalou = _atualizar_estagnacao(s, progresso)
    if escalou is not None:
        return s, escalou

    # Pergunta que uma ferramenta responde: responde e retoma a coleta na MESMA mensagem.
    # Vem antes das pendências porque a dúvida do lead é o que trava a conversa; a correção
    # pendente vira justamente o "retome a coleta" da diretiva.
    if intent is Intent.CONSULTA:
        return s, abs_.avisos + [_consulta(s, abs_)]

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
        return s, abs_.avisos + [SendText(text=_t("policy.txt_aguarde"))]

    return s, abs_.avisos + _fluxo(s, rules, today)


# --------------------------------------------------------------------------- consulta com ferramenta
def _consulta(s: LeadState, abs_: _Absorcao) -> Action:
    """Diretiva do turno de consulta: responder com a ferramenta e emendar a próxima pergunta.

    Não mexe em `stage` (a etapa da venda não anda por causa de uma dúvida), mas grava
    `ultima_pergunta`: a pergunta VAI junto na mesma mensagem, e o Extractor do turno seguinte
    precisa dela para desambiguar um "35".
    """
    pendente = next((a for a in abs_.pendencias if a.kind == "ask_field"), None)
    campo = pendente.campo if pendente is not None else _campo_faltante(s)
    if campo is not None:
        s.ultima_pergunta = campo
        proxima = _t(f"diretiva.{campo}")
    else:
        proxima = _t("policy.diretiva_pos_cotacao")
    return AnswerWithTools(directive=_t_ctx("responder.diretiva_consulta", proxima=proxima))


# --------------------------------------------------------------------------- absorção
def _absorver(s: LeadState, e: Extraction, rules: Rules, today: date) -> _Absorcao:
    """Grava no estado qualquer campo citado, em qualquer estágio (lead não segue roteiro)."""
    out = _Absorcao()
    # Toggle do Studio: com a pré-validação local desligada, a API de cotação vira a
    # única autoridade de regra (recusa chega como 422 e vira `Refuse` pós-cotação).
    # O ano-modelo continua sendo perguntado: aquilo é UX de coleta, não regra de negócio.
    pre_validacao = bool(_p("tools.rules.pre_validacao_local"))

    if e.idade is not None:
        violacao = rules.validate_idade(e.idade) if pre_validacao else None
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
            out.pendencias.append(AskField(campo="veiculo", motivo=_t("policy.motivo_ano_modelo")))
        else:
            violacao = rules.validate_veiculo_ano(e.veiculo_ano) if pre_validacao else None
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
            if s.cep_tentativas > _p("tools.policy.max_cep_tentativas"):
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
        if s.cep_tentativas > _p("tools.policy.max_cep_tentativas"):
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
        violacao = rules.validate_data_inicio(e.data_inicio) if pre_validacao else None
        if violacao is not None:
            out.avisos.append(SendText(text=_t("policy.txt_data_passada")))
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
    if s.cep_tentativas > _p("tools.policy.max_cep_tentativas"):
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
        if s.objecoes >= _p("tools.policy.objecoes_ate_handoff") or _pede_desconto(e):
            return _handoff(s, HandoffReason.NEGOCIACAO)
        return s, [Reply(directive=_t("policy.diretiva_objecao"))]

    if abs_.campos_alterados:
        return s, abs_.avisos + _fluxo(s, rules, today)

    if e.intent is Intent.ESCOLHER_PLANO:
        return s, abs_.avisos + [Reply(directive=_t("policy.diretiva_mesmo_plano"))]
    return s, abs_.avisos + [Reply(directive=_t("policy.diretiva_pos_cotacao"))]


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
    if s.turnos_sem_progresso >= _p("tools.policy.max_turnos_sem_progresso"):
        s, acoes = _handoff(s, HandoffReason.SEM_PROGRESSO)
        return s, acoes
    return s, None


def _com_estagnacao(
    s: LeadState, acoes: list[Action], progresso: bool
) -> tuple[LeadState, list[Action]]:
    s, escalou = _atualizar_estagnacao(s, progresso)
    return (s, escalou) if escalou is not None else (s, acoes)
