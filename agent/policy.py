"""Máquina de estados do agente: a ÚNICA camada que decide.

Pura por construção — recebe estado + extração e devolve um NOVO estado e uma
lista de ações. Sem I/O e sem LLM, porque decisão de venda (cotar, recusar,
escalar) precisa ser determinística, testável e auditável; o LLM só extrai
dados e transforma `AskField`/`Reply` em texto.

O lead pode cotar VÁRIOS carros de uma vez: `LeadState.veiculos` é a fonte da
verdade e `veiculo_texto/veiculo_ano/quote_result` são espelho de `veiculos[0]`,
sincronizado num ponto só (`_absorver`). Uma cotação por carro, mesmo plano
(`DoQuotes`), e uma resposta com todos (`PresentMany`).

O plano é perguntado UMA vez (`plano_perguntado` marca que a pergunta saiu): se a
resposta seguinte não trouxer a escolha, a policy assume `tools.policy.plano_padrao`
e marca `plano_assumido` — a troca depois recota todos os carros.

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
    AnswerAbout,
    AnswerWithTools,
    AskField,
    AskPlan,
    CampoColeta,
    ConfirmCep,
    DoQuotes,
    Extraction,
    Handoff,
    HandoffReason,
    Intent,
    LeadState,
    Present,
    PresentMany,
    QuoteOutcome,
    QuoteRequest,
    Refuse,
    Reply,
    SendText,
    Stage,
    VeiculoColetado,
    VeiculoExtraido,
)
from agent.runtime_config import store

if TYPE_CHECKING:  # pragma: no cover - só para type hints
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
TXT_CEP_AUSENTE = _default("policy.txt_cep_ausente")
TXT_MIDIA_2 = _default("policy.txt_midia_2")
DIRETIVA_OBJECAO = _default("policy.diretiva_objecao")
DIRETIVA_POS_COTACAO = _default("policy.diretiva_pos_cotacao")
DIRETIVA_MESMO_PLANO = _default("policy.diretiva_mesmo_plano")
MOTIVO_ANO_MODELO = _default("policy.motivo_ano_modelo")

STAGES_TERMINAIS = frozenset({Stage.HANDOFF, Stage.ENCERRADO, Stage.ENCERRADO_RECUSA})
# Terminais que a mensagem seguinte do lead ainda pode reabrir (o HANDOFF, não: lá já tem gente).
STAGES_REABRIVEIS = frozenset({Stage.ENCERRADO, Stage.ENCERRADO_RECUSA})

# Sinal do `conversation` para "estado terminal, não gastei o Extractor". Não é `None` (que é
# mídia sem texto) nem uma extração de verdade: é uma constante que a policy reconhece por
# identidade e trata como "não sei o que o lead disse".
TERMINAL_SEM_EXTRACAO = Extraction(intent=Intent.OUTRO, observacao="terminal_sem_extracao")

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
        # Saudação e dúvida sobre o produto movem a conversa: contá-las como estagnação era o que
        # mandava para o humano quem só tinha feito duas perguntas legítimas.
        Intent.SAUDACAO,
        Intent.DUVIDA_PRODUTO,
    }
)

_STAGE_DO_CAMPO: dict[CampoColeta, Stage] = {
    "idade": Stage.COLETA_IDADE,
    "veiculo": Stage.COLETA_VEICULO,
    "cep": Stage.COLETA_CEP,
    "plano": Stage.ESCOLHA_PLANO,
    "data_inicio": Stage.COLETA_DATA,
}

# Intents que NÃO são tentativa de responder a pergunta pendente: uma dúvida no meio da coleta
# do CEP não pode gastar uma das tentativas de CEP do lead.
_INTENTS_QUE_NAO_RESPONDEM = frozenset(
    {Intent.DUVIDA_PRODUTO, Intent.CONSULTA, Intent.OBJECAO_PRECO, Intent.PEDIR_DESCONTO, Intent.ACEITAR}
)

# Campo da violação (`Violation.campo`) → campo de coleta que a policy sabe perguntar.
_CAMPO_DA_VIOLACAO: dict[str, CampoColeta] = {
    "idade": "idade",
    "veiculo_ano": "veiculo",
    "cep": "cep",
    "data_inicio": "data_inicio",
}


@dataclass
class _Absorcao:
    """Resultado de absorver os dados da mensagem atual no estado."""

    avisos: list[Action] = field(default_factory=list)      # informativos, não bloqueiam
    pendencias: list[Action] = field(default_factory=list)  # correções, bloqueiam o fluxo
    refuse: Refuse | None = None                            # recusa de negócio, encerra
    campo_recusado: str | None = None                       # qual campo causou a recusa (permite reabrir)
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
    _migrar_veiculos(s)

    # Re-entradas do conversation (não são turno novo do lead).
    if extraction is None:
        if s.stage is Stage.COTANDO and _veiculos_cotados(s):
            return _pos_cotacao(s)
        # `cep_confirmacao_pedida` separa a re-entrada (o conversation acabou de trazer o ViaCEP)
        # da MÍDIA que chega depois — as duas chegam aqui como `None`. Sem essa distinção, o lead
        # que mandava áudio na confirmação do CEP levava a mesma pergunta para sempre.
        if (
            s.stage is Stage.CONFIRMA_CEP
            and s.cep_info is not None
            and not s.cep_confirmado
            and not s.cep_confirmacao_pedida
        ):
            acoes = _resolver_cep(s)
            return (s, acoes) if acoes is not None else (s, _fluxo(s, rules, today))

    s.turnos += 1

    if s.stage in STAGES_TERMINAIS:
        return _terminal(s, extraction, rules, today)

    if extraction is None:
        return _midia(s)

    # Extração indisponível (LLM fora): não sabemos o que o lead disse, então NÃO
    # re-executamos o fluxo — foi assim que o lead levou a lista de planos 3x seguidas.
    # Pede para repetir; N seguidas viram handoff `sistema_instavel` (e nunca "não consigo
    # te ajudar": o problema é nosso, não do lead).
    if extraction.indisponivel:
        s.indisponiveis += 1
        if s.indisponiveis >= int(_p("tools.policy.max_indisponivel")):
            return _handoff(s, HandoffReason.SISTEMA_INSTAVEL)
        return _com_estagnacao(s, [SendText(text=_t("policy.txt_instabilidade"))], progresso=False)

    # Chegou texto legível: os contadores de degradação zeram.
    s.indisponiveis = 0
    s.midias = 0

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
        s.recusa_campo = abs_.campo_recusado   # é por ele que a correção seguinte reabre a conversa
        return s, [abs_.refuse]

    progresso = abs_.campos_alterados or intent in INTENTS_UTEIS

    # Plano perguntado UMA vez: qualquer resposta que não traga a escolha (inclusive "tanto
    # faz", ou o lead adiantando outro campo) faz a policy assumir o padrão e seguir.
    # A marca é `plano_perguntado`, e não a última pergunta: uma pendência no meio (um CEP
    # inválido respondendo à pergunta do plano) trocaria `ultima_pergunta` e faria o agente
    # perguntar o plano de novo. Como a marca só é gravada ao PERGUNTAR, nunca se assume antes.
    if s.plano_id is None and s.plano_perguntado and _campo_faltante(s) == "plano":
        progresso = _assumir_plano(s, rules) or progresso

    # Mesma regra para a data: perguntada uma vez, resposta que não traz data vira "hoje" — e o
    # `data_assumida` faz a apresentação dizer isso em voz alta, em vez de o pro-rata aparecer
    # do nada por causa de uma data que ninguém escolheu.
    if s.data_inicio is None and not s.data_assumida and s.data_perguntada and _campo_faltante(s) == "data_inicio":
        progresso = _assumir_data(s) or progresso

    s, escalou = _atualizar_estagnacao(s, progresso)
    if escalou is not None:
        return s, escalou

    # Pergunta que uma ferramenta responde: responde e retoma a coleta na MESMA mensagem.
    # Vem antes das pendências porque a dúvida do lead é o que trava a conversa; a correção
    # pendente vira justamente o "retome a coleta" da diretiva.
    if intent is Intent.CONSULTA:
        return s, abs_.avisos + [_consulta(s, abs_)]

    # Dúvida sobre o PRODUTO (plano, cobertura, franquia, carência, preço): responde com os dados
    # reais e retoma a coleta. Antes isso caía em `outro` e o agente enrolava ou inventava.
    if intent is Intent.DUVIDA_PRODUTO:
        return s, abs_.avisos + [_duvida_produto(s, abs_, rules)]

    if abs_.pendencias:
        return s, abs_.avisos + abs_.pendencias

    # CEP recém-informado ou ainda não confirmado.
    if s.stage is Stage.CONFIRMA_CEP and not s.cep_confirmado:
        _absorver_confirmacao_neutra(s, intent)
        acoes = _resolver_cep(s)
        if acoes is not None:
            return s, abs_.avisos + acoes

    if s.stage is Stage.APRESENTADO:
        return _pos_apresentacao(s, extraction, abs_, rules, today)

    # Mensagem que chegou enquanto a cotação está em voo e não mudou nada.
    if s.stage is Stage.COTANDO and s.quote_result is None and not abs_.campos_alterados:
        return s, abs_.avisos + [SendText(text=_t("policy.txt_aguarde"))]

    return s, abs_.avisos + _fluxo(s, rules, today)


# --------------------------------------------------------------------------- estados terminais
def _terminal(
    s: LeadState, extraction: Extraction | None, rules: Rules, today: date
) -> tuple[LeadState, list[Action]]:
    """Responde UMA vez ao estado terminal, e depois silencia.

    Silenciar é o que faltava no incidente do WhatsApp: 23 eventos de protocolo viraram 23
    mensagens idênticas para um número real. Antes do silêncio vêm as duas saídas legítimas:
    pedir humano (vale em qualquer etapa) e a reabertura de um encerramento.
    """
    util = (
        extraction is not None
        and extraction is not TERMINAL_SEM_EXTRACAO
        and not extraction.indisponivel
    )
    if util and extraction.intent is Intent.PEDIR_HUMANO and s.stage is not Stage.HANDOFF:
        return _handoff(s, HandoffReason.LEAD_PEDIU_HUMANO)

    if util and s.stage in STAGES_REABRIVEIS and _quer_reabrir(s, extraction):
        return _reabertura(s, extraction, rules, today)

    if s.terminal_avisado:
        return s, []            # já avisamos uma vez; o humano é quem fala agora
    s.terminal_avisado = True
    terminal = "handoff" if s.stage is Stage.HANDOFF else "encerrado"
    return s, [SendText(text=_t(f"policy.txt_terminal_{terminal}"))]


def _midia(s: LeadState) -> tuple[LeadState, list[Action]]:
    """Mensagem sem texto. Insistir no mesmo pedido não resolve: na Nª, chama gente."""
    s.midias += 1
    if s.midias >= int(_p("tools.policy.max_midias")):
        s, acoes = _handoff(s, HandoffReason.SEM_PROGRESSO)
        # O texto do handoff por mídia é outro; quem renderiza é o presenter, pelo slot indicado.
        acoes[0].payload["texto_slot"] = "presenter.handoff.so_midia"
        return s, acoes
    texto = _t("policy.txt_midia_2") if s.midias >= 2 else _t("policy.txt_midia")
    return _com_estagnacao(s, [SendText(text=texto)], progresso=False)


# --------------------------------------------------------------------------- perguntas do lead
def _proxima_pergunta(s: LeadState, abs_: _Absorcao) -> str:
    """O que o agente retoma depois de responder: a pendência, o próximo campo, ou o pós-cotação.

    Marca `ultima_pergunta`/`plano_perguntado` porque a pergunta VAI junto na mesma mensagem — o
    Extractor do turno seguinte precisa saber o que foi perguntado para desambiguar um "35".
    """
    pendente = next((a for a in abs_.pendencias if a.kind == "ask_field"), None)
    campo = pendente.campo if pendente is not None else _campo_faltante(s)
    if campo is None:
        return _t("policy.diretiva_pos_cotacao")
    s.ultima_pergunta = campo
    if campo == "plano":
        s.plano_perguntado = True   # foi perguntado pelo Responder; não se repete no template
    return _t(f"diretiva.{campo}")


def _consulta(s: LeadState, abs_: _Absorcao) -> Action:
    """Diretiva do turno de consulta: responder com a ferramenta e emendar a próxima pergunta."""
    return AnswerWithTools(
        directive=_t_ctx("responder.diretiva_consulta", proxima=_proxima_pergunta(s, abs_))
    )


def _duvida_produto(s: LeadState, abs_: _Absorcao, rules: Rules) -> Action:
    """Diretiva da dúvida sobre o produto: os dados REAIS dos planos + a próxima pergunta.

    A lista vai no prompt (e não numa mensagem de template) porque a resposta tem de caber na
    conversa: o lead perguntou uma coisa e continua sendo perguntado outra na mesma mensagem.
    """
    proxima = _proxima_pergunta(s, abs_)
    return AnswerAbout(
        directive=_t_ctx("responder.diretiva_duvida", planos=_dados_dos_planos(rules), proxima=proxima)
    )


def _dados_dos_planos(rules: Rules) -> str:
    """Planos + carência em texto, direto do `/planos` (o presenter formata; aqui não há preço)."""
    from agent.presenter import resumo_dos_planos

    carencia = (getattr(rules, "planos", None) or {}).get("regras", {}).get("carencia")
    return resumo_dos_planos(rules.planos_resumo(), carencia)


# --------------------------------------------------------------------------- reabertura após recusa
_CAMPO_LEGIVEL = {"idade": "a idade", "veiculo_ano": "o ano do carro"}


def _quer_reabrir(s: LeadState, e: Extraction) -> bool:
    """A mensagem mostra vontade de cotar de novo?

    Três sinais, do mais forte ao mais fraco: a correção do campo recusado, um dado novo
    qualquer, ou um intent que só existe dentro de uma cotação (escolher plano, fornecer dados).
    "Quero cotar outro carro" e "cota pro meu pai" chegam por um destes — o Extractor traz o
    carro/a idade, ou classifica como `fornecer_dados`.
    """
    if _traz_correcao(s, e):
        return True
    if e.intent in (Intent.ESCOLHER_PLANO, Intent.FORNECER_DADOS):
        return True
    return _traz_dado_novo(e)


def _traz_dado_novo(e: Extraction) -> bool:
    return (
        e.idade is not None
        or e.cep is not None
        or e.plano_id is not None
        or e.data_inicio is not None
        or bool(_veiculos_da_extracao(e))
    )


def _reabertura(
    s: LeadState, e: Extraction, rules: Rules, today: date
) -> tuple[LeadState, list[Action]]:
    """Volta o encerramento para a coleta e segue o roteiro na MESMA mensagem."""
    correcao = _traz_correcao(s, e)
    campo = s.recusa_campo or "o dado"
    # Sem correção do campo recusado, o valor velho é apagado: "quero cotar outro carro" tem de
    # perguntar o carro de novo, não recotar o que acabou de ser recusado.
    _reabrir(s, limpar=None if correcao else s.recusa_campo)
    abs_ = _absorver(s, e, rules, today)
    if abs_.refuse is not None:          # corrigiu para outro valor inválido: recusa de novo
        s.stage = Stage.ENCERRADO_RECUSA
        s.recusa_campo = abs_.campo_recusado
        return s, [abs_.refuse]
    contexto = (
        _t_ctx("policy.diretiva_reabertura", campo=_CAMPO_LEGIVEL.get(campo, campo))
        if correcao
        else None
    )
    return s, abs_.avisos + _fluxo(s, rules, today, contexto=contexto)


def _traz_correcao(s: LeadState, e: Extraction) -> bool:
    """A mensagem traz um valor NOVO para o campo que causou a recusa?

    Válido ou não: quem decide é a absorção logo em seguida. Corrigir para outro valor fora da
    faixa merece a recusa de novo (com o motivo), não a frase de "atendimento encerrado".
    """
    if s.recusa_campo == "idade":
        return e.idade is not None
    if s.recusa_campo == "veiculo_ano":
        return any(v.ano is not None for v in _veiculos_da_extracao(e))
    return False


def _reabrir(s: LeadState, limpar: str | None = None) -> None:
    """Volta a conversa para a coleta: o erro de digitação não pode custar a venda."""
    s.stage = Stage.INICIO
    s.handoff_reason = None
    s.recusa_campo = None
    s.turnos_sem_progresso = 0
    s.terminal_avisado = False
    if limpar == "idade":
        s.idade = None
    elif limpar == "veiculo_ano":
        s.veiculos = []
        s.quote_result = None
        _sincronizar_veiculo(s)


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
            out.campo_recusado = "idade"
            return out
        out.campos_alterados = out.campos_alterados or s.idade != e.idade
        s.idade = e.idade

    if _absorver_veiculos(s, e, rules, today, out, pre_validacao) is not None:
        return out

    cep8 = rules.normalize_cep(e.cep) if e.cep is not None else None
    # A pergunta pendente é o CEP? É por turno, não por mensagem com "cep" dentro: o lead que
    # responde outra coisa também consumiu uma tentativa (era assim que dois CEPs inválidos viravam pergunta infinita).
    pergunta_era_cep = (
        s.stage is Stage.COLETA_CEP or s.ultima_pergunta == "cep"
    ) and e.intent not in _INTENTS_QUE_NAO_RESPONDEM

    if cep8 is not None:
        out.campos_alterados = True
        s.cep = cep8
        s.cep_info = None
        s.cep_confirmado = False
        s.cep_confirmacao_pedida = False
        s.cep_ausente = False
        s.cep_neutros = 0
        s.stage = Stage.CONFIRMA_CEP
    elif e.intent is Intent.CONFIRMAR and s.stage is Stage.CONFIRMA_CEP:
        s.cep_confirmado = True
        out.campos_alterados = True
    elif e.intent is Intent.NEGAR and s.stage is Stage.CONFIRMA_CEP:
        s.cep = None
        s.cep_info = None
        s.cep_confirmado = False
        s.cep_confirmacao_pedida = False
        out.campos_alterados = True
        _falhou_o_cep(s, out, "lead disse que o CEP está errado; pedir de novo")
    elif e.intent is Intent.NAO_SEI and (s.stage is Stage.COLETA_CEP or s.ultima_pergunta == "cep"):
        s.cep_ausente = True
        out.campos_alterados = True
    elif e.cep is not None or (pergunta_era_cep and s.cep is None and not s.cep_ausente):
        motivo = "formato inválido" if e.cep is not None else "o lead não mandou o CEP; pedir de novo"
        _falhou_o_cep(s, out, motivo)

    if e.plano_id is not None:
        out.campos_alterados = out.campos_alterados or s.plano_id != e.plano_id
        s.plano_id = e.plano_id
        s.plano_assumido = False   # escolha do lead vence o padrão assumido antes

    _absorver_data(s, e, rules, out, pre_validacao)
    return out


def _falhou_o_cep(s: LeadState, out: _Absorcao, motivo: str) -> None:
    """Mais um turno sem CEP válido. No teto, cota sem CEP e DIZ isso — nada de pedir de novo."""
    s.cep_tentativas += 1
    if s.cep_tentativas >= int(_p("tools.policy.max_cep_tentativas")):
        s.cep_ausente = True
        out.campos_alterados = True
        out.avisos.append(SendText(text=_t("policy.txt_cep_ausente")))
        return
    s.stage = Stage.COLETA_CEP
    s.ultima_pergunta = "cep"
    out.pendencias.append(AskField(campo="cep", motivo=motivo))


def _absorver_confirmacao_neutra(s: LeadState, intent: Intent) -> None:
    """Resposta neutra à confirmação do CEP: na 2ª, dá o CEP por bom e segue.

    Repetir "é aí que o carro fica?" três vezes é o mesmo beco do CEP inválido, com outro nome.
    """
    if intent in (Intent.CONFIRMAR, Intent.NEGAR):
        return
    s.cep_neutros += 1
    if s.cep_neutros >= 2:
        s.cep_confirmado = True


def _como_data(valor: Any) -> date | None:
    """`data_inicio` pode chegar como `date` (pydantic) ou como string (store externo, replay)."""
    if isinstance(valor, date):
        return valor
    if isinstance(valor, str):
        try:
            return date.fromisoformat(valor.strip()[:10])
        except ValueError:
            return None
    return None


def _absorver_data(
    s: LeadState, e: Extraction, rules: Rules, out: _Absorcao, pre_validacao: bool
) -> None:
    """Data de início: valor explícito, data no passado (avisa e re-pergunta) ou "tanto faz"."""
    data = _como_data(e.data_inicio)
    if data is not None and not e.data_vaga:
        violacao = rules.validate_data_inicio(data) if pre_validacao else None
        if violacao is None:
            # A mesma data que já estava assumida ("pode começar hoje" depois de cotar com hoje)
            # não é mudança: recotar e reenviar a mesma mensagem é o que o lead lê como eco.
            out.campos_alterados = out.campos_alterados or s.data_inicio != data
            s.data_inicio = data
            s.data_assumida = False
            return
        # "quero que comece ontem": avisa E pergunta de novo (antes sumia em silêncio).
        out.avisos.append(SendText(text=_t("policy.txt_data_passada")))
        s.stage = Stage.COLETA_DATA
        s.ultima_pergunta = "data_inicio"
        s.data_perguntada = True
        out.pendencias.append(
            AskField(campo="data_inicio", motivo="a data que o lead deu já passou; peça outra")
        )
        return

    vago = e.data_vaga or (e.intent is Intent.NAO_SEI and s.ultima_pergunta == "data_inicio")
    if vago and s.data_inicio is None and not s.data_assumida:
        _assumir_data(s)
        out.campos_alterados = True


# --------------------------------------------------------------------------- veículos
def _veiculos_da_extracao(e: Extraction) -> list[VeiculoExtraido]:
    """Carros citados na mensagem. Sem a lista, os campos escalares valem como um carro."""
    if e.veiculos:
        return e.veiculos
    if e.veiculo_texto is not None or e.veiculo_ano is not None:
        return [VeiculoExtraido(texto=e.veiculo_texto, ano=e.veiculo_ano, ano_parece_modelo=e.ano_parece_modelo)]
    return []


def _casar_veiculo(coletados: list[VeiculoColetado], novo: VeiculoExtraido) -> int | None:
    """Índice do carro que o lead está completando/corrigindo; `None` = é um carro novo.

    Casa pelo texto ("o HB20 é 2020" volta no HB20); um ano solto completa o primeiro carro
    sem ano, ou corrige o único carro da conversa — nunca inventa um carro sem nome.
    """
    if novo.texto:
        alvo = novo.texto.strip().lower()
        for i, v in enumerate(coletados):
            atual = (v.texto or "").strip().lower()
            if atual and (alvo in atual or atual in alvo):
                return i
        return None
    if novo.ano is None:
        return None
    for i, v in enumerate(coletados):
        if v.ano is None:
            return i
    return 0 if len(coletados) == 1 else None


def _absorver_veiculos(
    s: LeadState, e: Extraction, rules: Rules, today: date, out: _Absorcao, pre_validacao: bool
) -> Refuse | None:
    """Funde os carros da mensagem em `s.veiculos` e sincroniza o espelho de 1 carro.

    Devolve a recusa (e para tudo) quando um carro fere a regra de aceitação; ano suspeito de
    ano-modelo vira pendência, como no fluxo entregue.
    """
    novos = _veiculos_da_extracao(e)
    if not novos:
        return None
    teto = int(_p("tools.policy.max_veiculos"))

    for novo in novos:
        ano = novo.ano
        if ano is not None and (novo.ano_parece_modelo or ano > today.year):
            # Não grava: 2027 quase sempre é ano-modelo, e ano errado vira preço errado.
            s.stage = Stage.COLETA_VEICULO
            s.ultima_pergunta = "veiculo"
            out.pendencias.append(AskField(campo="veiculo", motivo=_t("policy.motivo_ano_modelo")))
            ano = None
        elif ano is not None and pre_validacao:
            violacao = rules.validate_veiculo_ano(ano)
            if violacao is not None:
                out.refuse = Refuse(motivo=violacao.motivo)
                out.campo_recusado = "veiculo_ano"
                return out.refuse

        indice = _casar_veiculo(s.veiculos, novo)
        if indice is None:
            if len(s.veiculos) >= teto:
                out.avisos.append(SendText(text=_t_ctx("policy.txt_max_veiculos", max=teto)))
                continue
            if novo.texto is None and ano is None:
                continue
            s.veiculos.append(VeiculoColetado(texto=novo.texto, ano=ano))
            out.campos_alterados = True
            continue

        alvo = s.veiculos[indice]
        if novo.texto and novo.texto != alvo.texto:
            alvo.texto = novo.texto
            out.campos_alterados = True
        if ano is not None and ano != alvo.ano:
            alvo.ano = ano
            out.campos_alterados = True

    _sincronizar_veiculo(s)
    return None


def _migrar_veiculos(s: LeadState) -> None:
    """Estado que só tem os campos escalares (conversa antiga, teste, store externo) vira lista.

    Depois daqui o resto da policy lê SÓ `s.veiculos` — um caminho, não dois.
    """
    if s.veiculos or (s.veiculo_texto is None and s.veiculo_ano is None):
        return
    s.veiculos = [VeiculoColetado(texto=s.veiculo_texto, ano=s.veiculo_ano, quote_result=s.quote_result)]


def _sincronizar_veiculo(s: LeadState) -> None:
    """ÚNICO ponto de espelho: `veiculo_texto/veiculo_ano` são sempre o primeiro carro."""
    primeiro = s.veiculos[0] if s.veiculos else None
    s.veiculo_texto = primeiro.texto if primeiro else None
    s.veiculo_ano = primeiro.ano if primeiro else None


def _rotulos(s: LeadState) -> str:
    return "; ".join(v.rotulo() for v in s.veiculos)


def _veiculos_cotados(s: LeadState) -> list[VeiculoColetado]:
    """Carros que já têm cotação (a migração garante que a lista existe)."""
    return [v for v in s.veiculos if v.quote_result is not None]


# --------------------------------------------------------------------------- plano
def _assumir_plano(s: LeadState, rules: Rules) -> bool:
    """Assume o plano padrão em vez de repetir a pergunta. Padrão inválido cai no 1º da API."""
    ids = [p.id for p in rules.planos_resumo()]
    padrao = str(_p("tools.policy.plano_padrao"))
    if padrao not in ids:
        padrao = ids[0]
    s.plano_id = padrao
    s.plano_assumido = True
    return True


def _plano_valido(s: LeadState, rules: Rules) -> bool:
    """O plano guardado ainda existe no `/planos` corrente? (o catálogo muda no meio da conversa)."""
    return s.plano_id is not None and s.plano_id in rules.plano_ids()


# --------------------------------------------------------------------------- data de início
def _assumir_data(s: LeadState) -> bool:
    """O lead não quis escolher: vigência a partir de hoje, e a apresentação diz isso."""
    s.data_assumida = True
    s.data_perguntada = True
    return True


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
        s.cep_confirmacao_pedida = True
        return [ConfirmCep(cep=s.cep or info.cep, cidade=info.cidade or "", uf=info.uf or "")]

    s.cep_tentativas += 1
    if s.cep_tentativas >= int(_p("tools.policy.max_cep_tentativas")):
        s.cep_confirmado = True  # segue com o CEP como está (o formato é válido; o ViaCEP é que não achou)
        return None
    s.stage = Stage.COLETA_CEP
    s.ultima_pergunta = "cep"
    return [AskField(campo="cep", motivo="não encontrei esse CEP; pedir de novo")]


# --------------------------------------------------------------------------- fluxo de coleta
def _campo_faltante(s: LeadState) -> CampoColeta | None:
    """Ordem de coleta: idade → plano → veículo(s) → CEP.

    O plano vem cedo porque ele vale para TODOS os carros do lead: saber o plano antes de
    saber quantos carros são é o que permite cotar todos de uma vez.
    """
    if s.idade is None:
        return "idade"
    if s.plano_id is None:
        return "plano"
    if not s.veiculos or any(v.ano is None for v in s.veiculos):
        return "veiculo"
    if s.cep is None and not s.cep_ausente:
        return "cep"
    # A data é o ÚLTIMO campo: sem perguntá-la, o pro-rata do primeiro mês aparecia na cotação
    # sem que ninguém tivesse falado de data ("pro-rata fantasma" da auditoria).
    if s.data_inicio is None and not s.data_assumida:
        return "data_inicio"
    return None


def _contexto_da_pergunta(s: LeadState, campo: CampoColeta) -> str | None:
    """Contexto para o Responder: qual carro falta, ou que a cotação é de vários carros."""
    if campo == "veiculo":
        sem_ano = next((v for v in s.veiculos if v.ano is None), None)
        if sem_ano is not None and len(s.veiculos) > 1:
            return _t_ctx("policy.motivo_ano_carro", carro=sem_ano.rotulo())
        return None
    if len(s.veiculos) > 1:
        return _t_ctx("policy.diretiva_multiplos", carros=_rotulos(s))
    return None


def _fluxo(s: LeadState, rules: Rules, today: date, contexto: str | None = None) -> list[Action]:
    """Pergunta o próximo campo faltante ou dispara a cotação de TODOS os carros.

    `contexto` é uma ponte para o Responder (ex.: "o lead corrigiu a idade; agradeça e siga") que
    entra como `motivo` da pergunta — o mesmo caminho do contexto de vários carros.
    """
    campo = _campo_faltante(s)
    if campo is not None:
        s.stage = _STAGE_DO_CAMPO[campo]
        s.ultima_pergunta = campo
        if campo == "plano":
            s.plano_perguntado = True
            return [AskPlan(planos=rules.planos_resumo())]
        if campo == "data_inicio":
            s.data_perguntada = True
        return [AskField(campo=campo, motivo=contexto or _contexto_da_pergunta(s, campo))]

    # O plano guardado pode ter sumido do catálogo (o `/planos` é relido a cada TTL): perguntar
    # de novo é melhor que mandar um `plano_id` que a API vai recusar com 422.
    if not _plano_valido(s, rules):
        s.plano_id = None
        s.plano_assumido = False
        s.plano_perguntado = True
        s.stage = Stage.ESCOLHA_PLANO
        s.ultima_pergunta = "plano"
        return [AskPlan(planos=rules.planos_resumo())]

    s.stage = Stage.COTANDO
    s.quote_result = None  # cotação nova: o resultado antigo não vale mais
    for veiculo in s.veiculos:
        veiculo.quote_result = None
    cep = None if s.cep_ausente else s.cep
    data_inicio = (s.data_inicio or today).isoformat()
    requests = [
        QuoteRequest(
            plano_id=s.plano_id,
            idade=s.idade,
            veiculo_ano=veiculo.ano,
            cep=cep,
            data_inicio=data_inicio,
        )
        for veiculo in s.veiculos
    ]
    # Última checagem antes de gastar a chamada (o que a docstring de `models.py` sempre prometeu).
    # Com a pré-validação local desligada no Studio, quem julga é a API — e só ela.
    if bool(_p("tools.rules.pre_validacao_local")):
        violacoes = [v for req in requests for v in rules.validate_request(req)]
        if violacoes:
            return _corrigir_antes_de_cotar(s, violacoes[0])
    return [DoQuotes(requests=requests)]


def _corrigir_antes_de_cotar(s: LeadState, violacao: Any) -> list[Action]:
    """Inconsistência que sobrou até o `QuoteRequest`: apaga o valor e pergunta de novo.

    Nunca deveria acontecer (a absorção já valida campo a campo); se acontecer, o lead recebe
    uma pergunta em vez de um 422 na cara.
    """
    campo = _CAMPO_DA_VIOLACAO.get(violacao.campo, "idade")
    if violacao.campo == "idade":
        s.idade = None
    elif violacao.campo == "cep":
        s.cep = None
        s.cep_info = None
        s.cep_confirmado = False
        s.cep_confirmacao_pedida = False
    elif violacao.campo == "data_inicio":
        s.data_inicio = None
        s.data_assumida = False
    s.stage = _STAGE_DO_CAMPO[campo]
    s.ultima_pergunta = campo
    return [AskField(campo=campo, motivo=violacao.motivo)]


# --------------------------------------------------------------------------- pós-cotação
def _pos_cotacao(s: LeadState) -> tuple[LeadState, list[Action]]:
    """Traduz os resultados da API em ação. Preço só aparece aqui, vindo do `Quote`.

    Três baldes: o que cotou vira apresentação (um carro = `Present`, vários = `PresentMany`,
    com os recusados e os pendentes citados linha a linha); sem NENHUM cotado, vale o
    comportamento entregue — recusa se todos foram recusados, humano no resto.
    """
    cotados = _veiculos_cotados(s)
    if not cotados:  # defensivo: só chamado com resultado já gravado
        return _handoff(s, HandoffReason.ERRO_INTERNO)

    if any(v.quote_result.outcome is QuoteOutcome.OK for v in cotados):
        s.stage = Stage.APRESENTADO
        if len(cotados) == 1:
            return s, [
                Present(
                    result=cotados[0].quote_result,
                    cep_ausente=s.cep_ausente,
                    data_assumida=s.data_assumida,
                )
            ]
        return s, [
            PresentMany(resultados=cotados, cep_ausente=s.cep_ausente, data_assumida=s.data_assumida)
        ]

    if any(v.quote_result.outcome is QuoteOutcome.BUG for v in cotados):
        return _handoff(s, HandoffReason.ERRO_INTERNO)
    if any(v.quote_result.outcome is QuoteOutcome.INDISPONIVEL for v in cotados):
        return _handoff(s, HandoffReason.COTACAO_INDISPONIVEL)
    s.stage = Stage.ENCERRADO_RECUSA
    motivo = cotados[0].quote_result.motivo_recusa or "perfil fora dos nossos critérios"
    return s, [Refuse(motivo=motivo)]


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

    # "quero ver outro plano" sem dizer qual: mostra a vitrine. Antes o lead ouvia "você já está
    # nesse plano", que é resposta a uma pergunta que ele não fez.
    if e.intent is Intent.ESCOLHER_PLANO and e.plano_id is None:
        s.stage = Stage.ESCOLHA_PLANO
        s.ultima_pergunta = "plano"
        s.plano_perguntado = True
        return s, abs_.avisos + [AskPlan(planos=rules.planos_resumo())]

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
        "veiculos": [{"texto": v.texto, "ano": v.ano} for v in s.veiculos],
        "veiculo_texto": s.veiculo_texto,
        "veiculo_ano": s.veiculo_ano,
        "cep": s.cep,
        "cep_cidade": s.cep_info.cidade if s.cep_info else None,
        "cep_uf": s.cep_info.uf if s.cep_info else None,
        "cep_ausente": s.cep_ausente,
        "plano_id": s.plano_id,
        # A data que FOI para a API, não o campo vazio: o consultor precisa emitir com a mesma
        # vigência que gerou o preço (era isso que saía `null` com `request.data_inicio` preenchido).
        "data_inicio": _data_enviada(s),
        "data_assumida": s.data_assumida,
    }


def _data_enviada(s: LeadState) -> str | None:
    """A `data_inicio` do último request feito; sem cotação nenhuma, o que o lead informou."""
    for veiculo in s.veiculos:
        if veiculo.quote_result is not None:
            return veiculo.quote_result.request.data_inicio
    if s.quote_result is not None:
        return s.quote_result.request.data_inicio
    return s.data_inicio.isoformat() if s.data_inicio else None


def _payload_handoff(s: LeadState, reason: HandoffReason) -> dict[str, Any]:
    """O humano precisa receber tudo pronto: dados, cotação (se houver) e o porquê."""
    payload: dict[str, Any] = {
        "dados": _dados_coletados(s),
        "cotacao": None,
        "cotacoes": [],
        "motivo": reason.value,
        "conversation_id": s.conversation_id,
    }
    # `cotacao`/`quote_id` continuam sendo os do primeiro carro (o humano de hoje lê isso);
    # `cotacoes` é a lista completa, uma entrada por carro cotado.
    payload["cotacoes"] = [
        {"carro": v.rotulo(), "cotacao": v.quote_result.model_dump(mode="json")}
        for v in _veiculos_cotados(s)
    ]
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
