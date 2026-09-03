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
    "data_inicio": Stage.ESCOLHA_PLANO,
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
        if s.stage is Stage.CONFIRMA_CEP and s.cep_info is not None and not s.cep_confirmado:
            acoes = _resolver_cep(s)
            return (s, acoes) if acoes is not None else (s, _fluxo(s, rules, today))

    s.turnos += 1

    # Estado terminal: responde educado, sem reabrir a coleta — com duas exceções.
    if s.stage in STAGES_TERMINAIS:
        util = extraction is not None and not extraction.indisponivel
        # 1) Pedido explícito de humano vale em QUALQUER etapa (antes ele virava a frase de encerrado).
        if util and extraction.intent is Intent.PEDIR_HUMANO and s.stage is not Stage.HANDOFF:
            return _handoff(s, HandoffReason.LEAD_PEDIU_HUMANO)
        # 2) Recusa que o lead corrigiu na mensagem seguinte: reabre em vez de encerrar de novo.
        if util and s.stage is Stage.ENCERRADO_RECUSA and _traz_correcao(s, extraction):
            campo = s.recusa_campo or "o dado"
            _reabrir(s)
            abs_ = _absorver(s, extraction, rules, today)
            if abs_.refuse is not None:      # corrigiu para outro valor inválido: recusa de novo
                s.stage = Stage.ENCERRADO_RECUSA
                s.recusa_campo = abs_.campo_recusado
                return s, [abs_.refuse]
            contexto = _t_ctx("policy.diretiva_reabertura", campo=_CAMPO_LEGIVEL.get(campo, campo))
            return s, abs_.avisos + _fluxo(s, rules, today, contexto=contexto)
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
        acoes = _resolver_cep(s)
        if acoes is not None:
            return s, abs_.avisos + acoes

    if s.stage is Stage.APRESENTADO:
        return _pos_apresentacao(s, extraction, abs_, rules, today)

    # Mensagem que chegou enquanto a cotação está em voo e não mudou nada.
    if s.stage is Stage.COTANDO and s.quote_result is None and not abs_.campos_alterados:
        return s, abs_.avisos + [SendText(text=_t("policy.txt_aguarde"))]

    return s, abs_.avisos + _fluxo(s, rules, today)


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


def _reabrir(s: LeadState) -> None:
    """Volta a conversa para a coleta: o erro de digitação não pode custar a venda."""
    s.stage = Stage.INICIO
    s.handoff_reason = None
    s.recusa_campo = None
    s.turnos_sem_progresso = 0


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
        s.plano_assumido = False   # escolha do lead vence o padrão assumido antes

    if e.data_inicio is not None and not e.data_vaga:
        violacao = rules.validate_data_inicio(e.data_inicio) if pre_validacao else None
        if violacao is not None:
            out.avisos.append(SendText(text=_t("policy.txt_data_passada")))
        else:
            out.campos_alterados = out.campos_alterados or s.data_inicio != e.data_inicio
            s.data_inicio = e.data_inicio

    return out


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
    s.plano_id = padrao  # type: ignore[assignment]  # validado contra os ids do /planos
    s.plano_assumido = True
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
        return [AskField(campo=campo, motivo=contexto or _contexto_da_pergunta(s, campo))]

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
    return [DoQuotes(requests=requests)]


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
            return s, [Present(result=cotados[0].quote_result, cep_ausente=s.cep_ausente)]
        return s, [PresentMany(resultados=cotados, cep_ausente=s.cep_ausente)]

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
        "data_inicio": s.data_inicio.isoformat() if s.data_inicio else None,
    }


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
