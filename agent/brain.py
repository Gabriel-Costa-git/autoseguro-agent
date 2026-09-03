"""Camada LLM do agente: `Extractor` (mensagem → `Extraction`) e `Responder` (diretiva → texto).

O LLM faz só duas coisas: extrair dados e falar. Ele não decide nada (isso é da
`policy`) e nunca vê preço — o estado enviado no prompt é resumido sem valores e
`guard_resposta` é a última linha de defesa: preço só sai da API, renderizado pelo
`presenter`, e a resposta que contradiz o estado ou promete condição é descartada.

Toda chamada tem teto de tempo (`llm_timeout_s`) e devolve ao turno quanto custou
(`drenar_usage`: modelo, tokens de entrada/saída/cache e tentativas), porque o Extractor
roda em toda mensagem e era 81 % da conta de tokens.

Textos e parâmetros vêm do `runtime_config.store` NA CHAMADA (nunca no import), então
editar um prompt ou trocar o modelo no Studio vale no turno seguinte, sem reiniciar.
Os imports do agno continuam dentro dos construtores: as funções puras deste módulo
(prompts e guardrail) são testadas sem carregar o SDK nem exigir chave de API.

O Responder (e SÓ ele) oferece ao modelo as tools que o operador criou no painel
(`agent/tools_runtime.py`). Sem tool habilitada, o Agent é construído exatamente como na
entrega — sem `tools=` — e nada no turno muda.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import date
from pathlib import Path
from typing import Any, Self

from agent.config import settings
from agent.defaults import SLOTS
from agent.models import CampoColeta, Extraction, Intent, LeadState, QuoteOutcome
from agent.presenter import nomes_de_coberturas
from agent.runtime_config import ConfigError, store

log = logging.getLogger("autoseguro.brain")

Trace = Callable[[dict[str, Any]], None]

# Campos com slot próprio de fallback/diretiva; qualquer outro cai no texto padrão.
CAMPOS: tuple[str, ...] = ("idade", "veiculo", "cep", "plano", "data_inicio")

# Planos do catálogo entregue; só valem quando o `/planos` não chegou até aqui.
PLANOS_PADRAO = "essencial, completo, premium"

# --------------------------------------------------------------------------- parâmetros
# Estes três moram em `runtime_config._code_defaults()["settings"]`, que NÃO é escopo deste
# brief. Enquanto a chave não existir lá, `_param_settings` cai no padrão daqui — e quando ela
# existir (ou o operador puser um override no Studio), o store passa a mandar sem tocar no código.
RESPONDER_HISTORY_RUNS = 4      # 8 mandava meia conversa em cada chamada, sem ganho de contexto
LLM_TIMEOUT_S = 12.0            # teto por chamada: acima disso o lead está em silêncio


def _param_settings(nome: str, padrao: Any) -> Any:
    """Valor efetivo de `settings.<nome>`, com o padrão deste módulo quando o store não o tem.

    Só um override explícito do Studio ganha do padrão daqui: se o valor efetivo ainda é o
    default do `runtime_config` (o antigo), quem vale é este módulo.
    """
    try:
        efetivo = store.effective(f"settings.{nome}")
    except ConfigError:
        return padrao
    return efetivo["value"] if efetivo["origem"] == "override" else padrao


def responder_history_runs() -> int:
    return int(_param_settings("responder_history_runs", RESPONDER_HISTORY_RUNS))


def llm_timeout_s() -> float:
    return float(_param_settings("llm_timeout_s", LLM_TIMEOUT_S))


# Quem executa a tool é o agno, lá dentro do `arun`, com uma `Function` que foi construída
# junto com o Agent (cacheado e compartilhado entre conversas) — ela não sabe de qual turno
# veio a chamada. O vínculo é este ContextVar: `reply` publica a lista do turno antes de
# chamar o modelo, e cada execução de tool anota nela. Como cada turno roda na sua própria
# task asyncio, dois leads simultâneos não se misturam.
_TOOL_CALLS_DO_TURNO: ContextVar[list[dict[str, Any]] | None] = ContextVar("tool_calls_do_turno", default=None)

# Mesmo vínculo, mesma razão, para o que o guard do post_hook achou. NÃO pode ir por
# `dependencies`: o agno RESOLVE dependência que seja callable (chama sem argumento) e
# derruba o canal com um aviso — verificado no agno 3.0.5.
_GUARDS_DO_TURNO: ContextVar[list[dict[str, str]] | None] = ContextVar("guards_do_turno", default=None)

# --------------------------------------------------------------------------- guardrail
# Formas NUMÉRICAS de dinheiro: "R$", "209,90"/"209.90" e "reais". Dá para conferir o valor
# contra o material que nós mesmos demos ao modelo. O `(?!\d)` evita casar o meio de um CEP
# ("01.310-100") ou do próprio milhar ("1.025,14" casa só em "025,14").
_PRECO_RE = re.compile(r"R\$|\d+[,.]\d{2}(?!\d)|\bre[aá]is\b", re.IGNORECASE)

# Formas VAGAS: "uns 200", "de 100 a 300", "duzentos". Não têm valor conferível, então não há
# como serem citação do nosso material — se aparecem numa conversa sem cotação, são invenção.
_PRECO_VAGO_RE = re.compile(
    r"\buns\s+\d+"
    r"|\bde\s+\d{2,}\s+a\s+\d{2,}\b"
    r"|\b(?:cem|cento e|duzentos|trezentos|quatrocentos|quinhentos"
    r"|seiscentos|setecentos|oitocentos|novecentos)\b",
    re.IGNORECASE,
)


def fallback_text(campo: str | None) -> str:
    """Texto determinístico do campo pendente (slot `fallback.<campo>`)."""
    return store.text(f"fallback.{campo}") if campo in CAMPOS else store.text("fallback.padrao")


def contem_preco(texto: str) -> bool:
    """True se o texto tem cara de valor em dinheiro (R$, 1.234,56, 'reais')."""
    return bool(_PRECO_RE.search(texto or ""))


def contem_valor(texto: str) -> bool:
    """Como `contem_preco`, mais as formas vagas ("uns 200", "de 100 a 300", "duzentos")."""
    return contem_preco(texto) or bool(_PRECO_VAGO_RE.search(texto or ""))


def tem_cotacao_ok(state: LeadState) -> bool:
    """Alguma cotação da conversa voltou OK da API? (com N carros, basta uma)."""
    if state.quote_result is not None and state.quote_result.outcome is QuoteOutcome.OK:
        return True
    return any(
        v.quote_result is not None and v.quote_result.outcome is QuoteOutcome.OK
        for v in state.veiculos
    )


_VALOR_RE = re.compile(r"R\$\s*([\d.]+(?:,\d{2})?)|([\d.]+,\d{2})|(\d+(?:[.,]\d+)?)\s*re[aá]is", re.IGNORECASE)


def valores_citados(texto: str) -> set[float]:
    """Valores em dinheiro que o texto afirma (R$ 4.500,00, 209,90, "200 reais")."""
    achados: set[float] = set()
    for grupos in _VALOR_RE.findall(texto or ""):
        bruto = next((g for g in grupos if g), "")
        normalizado = bruto.replace(".", "").replace(",", ".").strip(".")
        try:
            achados.add(float(normalizado))
        except ValueError:
            continue
    return achados


def _valor_inventado(text: str, state: LeadState, permitido: str = "") -> str | None:
    """Trecho de dinheiro que a resposta não podia conter, ou `None` se está tudo certo.

    Libera em três casos, e só neles: não há dinheiro no texto; já existe cotação OK (o valor
    virou público pelo `presenter`); ou TODO valor citado veio do material que nós mesmos demos
    ao modelo na diretiva (`permitido`) — é o caso da dúvida sobre o produto, em que a franquia
    dos planos vai no prompt. Um valor a mais que o do material continua sendo invenção, e uma
    forma vaga ("uns 200") nunca é citação de material nenhum.
    """
    if tem_cotacao_ok(state):
        return None
    vago = _PRECO_VAGO_RE.search(text or "")
    if vago is not None:
        return vago.group(0)
    numerico = _PRECO_RE.search(text or "")
    if numerico is None:
        return None
    valores = valores_citados(text)
    if permitido and valores and valores <= valores_citados(permitido):
        return None
    return numerico.group(0)


def guard_price(text: str, state: LeadState, permitido: str = "") -> str:
    """Substitui a resposta do LLM por um fallback determinístico se ela citar dinheiro.

    Sem toggle e sem parâmetro de config: este é o guardrail da regra de ouro.
    """
    if _valor_inventado(text, state, permitido) is None:
        return text
    log.warning(
        "guardrail de preço disparou (conversation_id=%s, campo=%s)",
        state.conversation_id,
        state.ultima_pergunta,
    )
    return _fallback(state)


def _fallback(state: LeadState) -> str:
    """Fallback do campo pendente — nunca deixa o turno sem resposta."""
    return fallback_text(state.ultima_pergunta)


# --------------------------------------------------------------------------- validação pós-LLM
# O prompt é empurrão; isto é regra. Cada padrão nasceu de uma resposta REAL da auditoria da
# Fase 3, e a ação é sempre a mesma do `guard_price`: descartar a resposta e mandar o fallback
# determinístico da diretiva — o lead prefere uma pergunta seca a uma promessa que não existe.
_HISTORICO_INVENTADO_RE = re.compile(
    r"\bcomo (?:eu )?(?:te |lhe )?(?:disse|falei|comentei|expliquei|adiantei)\b"
    r"|\bcomo (?:j[áa] )?(?:falamos|conversamos|combinamos|vimos)\b"
    r"|\bconforme (?:te )?(?:disse|falei)\b",
    re.IGNORECASE,
)
_PROMESSA_RE = re.compile(
    r"\b(?:ajustar|reduzir|baixar|diminuir|flexibilizar|melhorar)\b[^.!?]{0,40}"
    r"\b(?:franquia|valor|pre[çc]o|parcela|mensalidade|condi[çc][ãa]o)\b"
    r"|\b(?:consigo|posso|d[áa] pra|vou (?:ver|tentar)|fa[çc]o)\b[^.!?]{0,30}"
    r"\b(?:desconto|abatimento|negociar|precinho)\b"
    r"|\bcondi[çc][ãa]o especial\b|\bfa[çc]o por\b",
    re.IGNORECASE,
)
_QUALIFICA_PLANO_RE = re.compile(
    r"\b(?:bem|muito|super|mais)\s+(?:completo|completa|vantajos[oa]|indicad[oa]|recomendad[oa])\b"
    r"|\bcusto[- ]benef[íi]cio\b"
    r"|\b[ée]\s+(?:o|a)\s+(?:melhor|ideal|mais indicad[oa])\b"
    r"|\bvale (?:muito )?a pena\b",
    re.IGNORECASE,
)

# Pergunta de um campo que já está no estado. A chave é a do estado; a palavra é o que precisa
# aparecer na DIRETIVA para a regra não disparar — perguntar de novo, quando a policy mandou,
# é correto (confirmação de ano-modelo, correção de CEP).
_PERGUNTA_DE_CAMPO: dict[str, tuple[re.Pattern[str], str]] = {
    "idade": (re.compile(r"(?:qual|quantos)[^.!?]{0,40}\b(?:idade|anos)\b[^.!?]{0,40}\?", re.IGNORECASE), "idade"),
    "veiculo_ano": (re.compile(r"(?:qual|que)[^.!?]{0,40}\bano\b[^.!?]{0,40}\?", re.IGNORECASE), "ano"),
    "cep": (
        re.compile(r"(?:qual|me (?:diz|passa|manda|informa)|informe)[^.!?]{0,40}\bcep\b[^.!?]{0,40}\?", re.IGNORECASE),
        "cep",
    ),
    "plano": (re.compile(r"(?:qual|quais|quer)[^.!?]{0,40}\bplano\b[^.!?]{0,40}\?", re.IGNORECASE), "plano"),
}


def _campos_preenchidos(state: LeadState) -> set[str]:
    """Campos que o agente NÃO pode perguntar de novo por conta própria."""
    cheios: set[str] = set()
    if state.idade is not None:
        cheios.add("idade")
    if state.veiculo_ano is not None or any(v.ano is not None for v in state.veiculos):
        cheios.add("veiculo_ano")
    if state.cep or state.cep_ausente:
        cheios.add("cep")
    if state.plano_id is not None:
        cheios.add("plano")
    return cheios


def _violacao(text: str, state: LeadState, directive: str) -> tuple[str, str] | None:
    """Primeira regra violada, como `(regra, trecho)`. `None` = a resposta pode sair."""
    texto = text or ""
    diretiva = (directive or "").lower()

    for campo in _campos_preenchidos(state):
        padrao, palavra = _PERGUNTA_DE_CAMPO[campo]
        if palavra in diretiva:
            continue                      # a policy PEDIU para perguntar isso de novo
        achado = padrao.search(texto)
        if achado is not None:
            return f"campo_ja_preenchido:{campo}", achado.group(0)

    if state.turnos <= 1:
        achado = _HISTORICO_INVENTADO_RE.search(texto)
        if achado is not None:
            return "historico_inexistente", achado.group(0)

    for regra, padrao in (("promessa", _PROMESSA_RE), ("qualifica_plano", _QUALIFICA_PLANO_RE)):
        achado = padrao.search(texto)
        if achado is not None:
            return regra, achado.group(0)

    trecho = _valor_inventado(texto, state, directive)
    if trecho is not None:
        return "valor_inventado", trecho
    return None


def guard_resposta(text: str, state: LeadState, directive: str = "") -> tuple[str, dict[str, str] | None]:
    """Valida a resposta do modelo contra o estado e as regras duras.

    Devolve `(texto, achado)`: com violação, o texto vira o fallback determinístico da diretiva
    e `achado` é o `{regra, trecho}` do evento `llm_guard`. Sem violação, o texto passa intacto.
    Roda no `post_hook` (antes de a resposta entrar no histórico da sessão) e de novo no
    `reply` — a garantia não pode depender de um hook cujas exceções o agno engole.
    """
    violacao = _violacao(text, state, directive)
    if violacao is None:
        return text, None
    regra, trecho = violacao
    log.warning(
        "llm_guard %s disparou (conversation_id=%s): %r", regra, state.conversation_id, trecho[:80]
    )
    return _fallback(state), {"regra": regra, "trecho": trecho[:160]}


# --------------------------------------------------------------------------- resiliência do LLM
# O provedor de LLM é a MESMA classe de dependência instável que a `/quote`: a cota
# gratuita do Gemini é 5 req/min e cada turno gasta 2 chamadas. Fato verificado no
# agno 3.0.5 (`agno/agent/_run.py`): `Agent.retries` é 0 por padrão, então o SDK NÃO
# re-tenta; e `arun` NÃO levanta — ele captura a exceção, marca
# `run.status = RunStatus.error` e coloca `str(exc)` em `run.content`. Por isso aqui
# se trata tanto a exceção quanto o run marcado como erro.
MAX_TENTATIVAS_LLM = 4          # 1 chamada + 3 novas tentativas (default; o Studio ajusta)
BUDGET_LLM_S = 30.0             # teto de espera somada; acima disso é melhor degradar
_BACKOFF_S = (2.0, 4.0, 8.0)    # usado quando o provedor não diz o `retryDelay`

# O 429 do Gemini chega como corpo JSON dentro da mensagem do `ModelProviderError`:
# {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [{... "retryDelay": "4s"}]}}
_RETRY_DELAY_RE = re.compile(r"retryDelay['\"]?:\s*['\"]?(\d+(?:\.\d+)?)s")
_CODE_RE = re.compile(r"['\"]?code['\"]?\s*:\s*(\d{3})")
_STATUS_TRANSITORIOS = frozenset({408, 409, 429, 500, 502, 503, 504})
_TEXTO_TRANSITORIO = re.compile(
    r"RESOURCE_EXHAUSTED|UNAVAILABLE|DEADLINE_EXCEEDED|rate.?limit|quota|overloaded|timed?.?out|try again",
    re.IGNORECASE,
)


class ChamadaLLMFalhou(RuntimeError):
    """Erro do provedor, venha ele levantado ou lido de um run marcado como ERROR."""

    def __init__(self, mensagem: str, status_code: int | None = None) -> None:
        super().__init__(mensagem)
        self.status_code = status_code


def _status_do_erro(exc: BaseException) -> int | None:
    """Status HTTP do erro: do atributo do agno ou do `"code"` no corpo da mensagem."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    achado = _CODE_RE.search(str(exc))
    return int(achado.group(1)) if achado else None


def _e_transitorio(exc: BaseException) -> bool:
    """Cota estourada, 5xx e timeout se re-tenta; 400/401/403 (erro nosso) não."""
    status = _status_do_erro(exc)
    if status is not None:
        return status in _STATUS_TRANSITORIOS
    if isinstance(exc, (TimeoutError, ConnectionError, asyncio.TimeoutError)):
        return True
    return bool(_TEXTO_TRANSITORIO.search(str(exc)))


def _retry_delay(exc: BaseException) -> float | None:
    """Espera pedida pelo provedor (`RetryInfo.retryDelay`), se ele disser."""
    achado = _RETRY_DELAY_RE.search(str(exc))
    return float(achado.group(1)) if achado else None


async def _com_retry[T](
    fn: Callable[[], Awaitable[T]],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_tentativas: int = MAX_TENTATIVAS_LLM,
    budget_s: float = BUDGET_LLM_S,
    clock: Callable[[], float] = time.monotonic,
    papel: str = "llm",
    emitir: Trace | None = None,
) -> T:
    """Repete `fn` enquanto a falha for transitória. Levanta a última exceção ao esgotar.

    Respeita o `retryDelay` do provedor quando ele existe (é o tempo que ele mesmo
    diz que a cota leva para liberar); senão, backoff 2 s → 4 s → 8 s.

    `emitir` recebe um `llm_retry` por espera e um `llm_error` na desistência: sem eles, a
    auditoria da Fase 3 viu 6 chamadas de mais de 20 s e nenhum evento no log explicando.
    """
    inicio = clock()
    tentativa = 1

    def _evento(nome: str, exc: BaseException, **extra: Any) -> None:
        if emitir is not None:
            emitir({
                "evento": nome,
                "papel": papel,
                "tentativa": tentativa,
                "status": _status_do_erro(exc) or type(exc).__name__,
                **extra,
            })

    while True:
        try:
            return await fn()
        except Exception as exc:
            if tentativa >= max_tentativas or not _e_transitorio(exc):
                _evento("llm_error", exc, erro=str(exc)[:200])
                raise
            espera = _retry_delay(exc)
            if espera is None:
                espera = _BACKOFF_S[min(tentativa, len(_BACKOFF_S)) - 1]
            if clock() - inicio + espera > budget_s:
                _evento("llm_error", exc, erro=str(exc)[:200], motivo="orcamento")
                raise
            log.warning(
                "%s: falha transitória (%s) na tentativa %d/%d; aguardando %.1fs",
                papel,
                _status_do_erro(exc) or type(exc).__name__,
                tentativa,
                max_tentativas,
                espera,
            )
            _evento("llm_retry", exc, espera_s=espera)
            await sleep(espera)
            tentativa += 1


def _erro_do_run(run: Any) -> str | None:
    """Mensagem de erro de um run que o agno marcou como ERROR (ele não levanta)."""
    status = getattr(run, "status", None)
    if str(getattr(status, "value", status) or "").upper() != "ERROR":
        return None
    conteudo = getattr(run, "content", None)
    return conteudo if isinstance(conteudo, str) and conteudo else "erro sem detalhe do provedor"


def _historico_do_run(run: Any) -> list[dict[str, Any]]:
    """`RunOutput.messages` (system + histórico + user + assistant) em forma serializável.

    `from_history=True` marca o que o agno puxou da sessão anterior — é o que deixa
    visível no Lab o que o modelo realmente recebeu além do prompt deste turno.
    """
    mensagens = getattr(run, "messages", None) or []
    out: list[dict[str, Any]] = []
    for m in mensagens:
        conteudo = getattr(m, "content", None)
        out.append(
            {
                "role": getattr(m, "role", "?"),
                "content": conteudo if isinstance(conteudo, str) else str(conteudo),
                "from_history": bool(getattr(m, "from_history", False)),
            }
        )
    return out


def system_do_historico(historico: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mensagens `system` que vieram do histórico da sessão — ou seja, prompts de turnos ANTIGOS.

    Tem de ser sempre vazio: o system prompt do turno é montado a cada chamada (`instructions`
    é um callable), e um prompt velho no histórico custaria centenas de tokens por chamada e
    poderia reintroduzir uma diretiva obsoleta. É o que `historico_kwargs` garante.
    """
    return [m for m in historico if m.get("role") == "system" and m.get("from_history")]


def historico_kwargs() -> dict[str, Any]:
    """Como o histórico do Responder é montado — e por que nenhum system prompt antigo entra.

    `num_history_runs` e `num_history_messages` são MUTUAMENTE EXCLUSIVOS no agno 3.0.5
    (`Agent.__init__` avisa e descarta o segundo), então não há como "passar os dois". Quem
    filtra o prompt velho é `system_message_role`: em `_messages.get_run_messages` ele vira o
    `skip_roles` do `session.get_messages`, e mensagem com esse papel não vem do histórico.
    Deixá-lo EXPLÍCITO aqui é o contrato: se um upgrade do agno mudar essa regra, o teste
    `test_historico_nunca_traz_system_do_passado` quebra antes de a conta de tokens subir.

    `num_history_runs` cai de 8 para 4: 8 runs mandavam meia conversa em cada chamada, e o
    que o Responder precisa saber do passado já está no `{resumo}` do estado.
    """
    return {
        "add_history_to_context": True,
        "num_history_runs": responder_history_runs(),
        "system_message_role": "system",
    }


def _usage_do_run(run: Any) -> dict[str, int] | None:
    """Tokens da chamada, de `RunOutput.metrics`. `None` quando o provedor não contou."""
    metrics = getattr(run, "metrics", None)
    if metrics is None:
        return None

    def _n(nome: str) -> int:
        valor = getattr(metrics, nome, 0)
        return int(valor) if isinstance(valor, (int, float)) else 0

    usage = {
        "input": _n("input_tokens"),
        "output": _n("output_tokens"),
        "total": _n("total_tokens"),
        "cache_read": _n("cache_read_tokens"),
    }
    return usage if any(usage.values()) else None


def _somar_usage(acumulado: dict[str, int] | None, novo: dict[str, int] | None) -> dict[str, int] | None:
    """Soma o usage das TENTATIVAS de uma chamada: o lead pagou por todas, não só pela que deu."""
    if novo is None:
        return acumulado
    if acumulado is None:
        return dict(novo)
    return {chave: acumulado.get(chave, 0) + valor for chave, valor in novo.items()}


# --------------------------------------------------------------------------- prompts
def lista_de_ferramentas(tools: list[Any]) -> str:
    """`- nome: descrição` de cada tool habilitada, para o prompt do Extractor."""
    return "\n".join(f"  - {t.nome}: {t.descricao}" for t in tools)


def _intent_exemplos(ferramentas: list[Any] | None = None) -> dict[Intent, str]:
    """Exemplos por intent, na ordem do enum (slots `intent.<valor>`).

    `consulta` só existe quando há tool habilitada: sem tool, o intent nem aparece no prompt
    (o prompt do Extractor fica byte-idêntico ao entregue) e a `Conversation` normaliza para
    `outro` se o modelo escolher o valor mesmo assim — ele está no schema do `Extraction`.
    `ferramentas=[]` força o caso "entregue" (é o que os goldens usam).
    """
    tools = store.custom_tools_habilitadas() if ferramentas is None else ferramentas
    exemplos: dict[Intent, str] = {}
    for i in Intent:
        if i is Intent.CONSULTA:
            if tools:
                exemplos[i] = store.text("intent.consulta", ferramentas=lista_de_ferramentas(tools))
            continue
        exemplos[i] = store.text(f"intent.{i.value}")
    return exemplos


def directive_for_field(campo: CampoColeta, motivo: str | None = None) -> str:
    """Traduz um `AskField` da policy na diretiva em linguagem natural do Responder."""
    base = store.text(f"diretiva.{campo}") if campo in CAMPOS else f"pergunte {campo}"
    return f"{base} (contexto: {motivo})" if motivo else base


def _resumo_carros(state: LeadState) -> str:
    """Um carro mantém o texto entregue; a partir de dois, a lista inteira (slot próprio)."""
    if len(state.veiculos) > 1:
        return store.text("brain.resumo_carros", carros="; ".join(v.rotulo() for v in state.veiculos))
    return f"carro: {state.veiculo_texto or '—'} (ano {state.veiculo_ano or '—'})"


def resumo_state(state: LeadState) -> str:
    """Resumo do estado para o prompt. NUNCA inclui valores da cotação — só o status."""
    partes = [
        f"idade: {state.idade if state.idade is not None else '—'}",
        _resumo_carros(state),
        f"cep: {state.cep or ('não sabe' if state.cep_ausente else '—')}",
        f"plano escolhido: {state.plano_id or '—'}",
        f"início desejado: {state.data_inicio.isoformat() if state.data_inicio else '—'}",
        f"etapa: {state.stage.value}",
    ]
    if state.cep_info and state.cep_info.cidade:
        partes.append(f"cidade do cep: {state.cep_info.cidade}/{state.cep_info.uf}")
    if state.quote_result is not None:
        partes.append(f"cotação: {state.quote_result.outcome.value} (o valor já foi enviado por outra mensagem)")
    return "; ".join(partes)


def ids_dos_planos(planos: dict | None = None) -> str:
    """Ids da vitrine CORRENTE, para o Extractor não aceitar um plano que não existe mais.

    Sai do `/planos` do boot quando ele está à mão; sem ele, do catálogo entregue.
    """
    ids = [str(p.get("id")) for p in (planos or {}).get("planos", []) if p.get("id")]
    return ", ".join(ids) or PLANOS_PADRAO


def build_extraction_instructions(
    state: LeadState, today: date, ferramentas: list[Any] | None = None, planos: dict | None = None
) -> str:
    """Prompt do Extractor (slot `extractor.instructions`): regras, intents e o bloco dinâmico.

    O bloco dinâmico (`{today}`, `{resumo}`, `{ultima}`) fica no FIM do slot de propósito:
    o prefixo é idêntico em toda chamada, que é a única forma de o provedor reaproveitar
    alguma coisa. (Cache explícito não compensa: o prefixo tem menos de 1.024 tokens.)

    `ferramentas` é ponto de injeção: `None` = lê as tools habilitadas do store (produção),
    `[]` = comportamento entregue, sem o intent `consulta`.
    """
    intents = "\n".join(f"- {i.value}: {ex}" for i, ex in _intent_exemplos(ferramentas).items())
    return store.text(
        "extractor.instructions",
        today=today.isoformat(),
        ano=today.year,
        planos=ids_dos_planos(planos),
        resumo=resumo_state(state),
        ultima=state.ultima_pergunta or "nenhuma",
        intents=intents,
    )


def coberturas_do_produto(planos: dict | None = None) -> str:
    """Lista legível das coberturas que existem, para o prompt do Responder.

    Sai do `/planos` quando ele está à mão (é a verdade do produto); sem ele, dos slots
    `presenter.cobertura.*`, que são o mesmo vocabulário que o presenter usa com o lead. É esta
    lista que impede o modelo de inventar "guincho".
    """
    chaves: list[str] = []
    for plano in (planos or {}).get("planos", []):
        chaves += list(plano.get("coberturas") or [])
    if not chaves:
        chaves = [k.removeprefix("presenter.cobertura.") for k in SLOTS if k.startswith("presenter.cobertura.")]
    return ", ".join(nomes_de_coberturas(chaves))


def build_responder_instructions(
    state: LeadState, directive: str, planos: dict | None = None
) -> str:
    """Prompt do Responder (slot `responder.instructions`): persona, estado, diretiva e guardrails.

    No PRIMEIRO turno a diretiva vem prefixada pela abertura (slot `responder.diretiva_abertura`):
    é o único lugar em que o agente se apresenta, e vale para qualquer diretiva do turno.
    """
    if state.turnos <= 1:
        directive = f"{store.text('responder.diretiva_abertura')} {directive}"
    return store.text(
        "responder.instructions",
        resumo=resumo_state(state),
        diretiva=directive,
        guardrails=store.text("responder.guardrails", coberturas=coberturas_do_produto(planos)),
    )


# --------------------------------------------------------------------------- resposta tipada
class RespostaLLM(str):
    """Texto do Responder + de onde ele veio (`llm`, `template` ou `fallback`).

    É `str` de propósito: o turno continua tratando a resposta como texto (o `conversation.py`
    não é escopo deste brief), e quem quiser saber a procedência lê `.source`. Sem isso o
    fallback determinístico saía no log rotulado `source="llm"` — foi o que a auditoria da
    Fase 3 achou em s07b.
    """

    __slots__ = ("source",)

    def __new__(cls, texto: str, source: str = "llm") -> Self:
        obj = super().__new__(cls, texto)
        obj.source = source
        return obj


# --------------------------------------------------------------------------- agentes agno
def _guard_hook(run_output: Any, run_context: Any) -> None:
    """post_hook do agno: valida a resposta ANTES dela entrar no histórico da sessão.

    Sem isso, uma resposta com preço (ou com uma promessa) ficaria gravada no histórico e
    contaminaria os turnos seguintes. Como o agno engole exceções de hook, o `Responder.reply`
    reaplica `guard_resposta` na saída — a garantia não pode depender do hook.
    """
    deps = getattr(run_context, "dependencies", None) or {}
    state = deps.get("state")
    if state is None or not isinstance(run_output.content, str):
        return
    texto, achado = guard_resposta(run_output.content, state, deps.get("directive", ""))
    run_output.content = texto
    achados = _GUARDS_DO_TURNO.get()
    if achado is not None and achados is not None:
        achados.append(achado)


def _extractor_instructions(run_context: Any) -> str:
    deps = getattr(run_context, "dependencies", None) or {}
    return build_extraction_instructions(deps["state"], deps["today"], planos=deps.get("planos"))


def _responder_instructions(run_context: Any) -> str:
    deps = getattr(run_context, "dependencies", None) or {}
    return build_responder_instructions(deps["state"], deps["directive"], deps.get("planos"))


class _AgenteLLM:
    """Base dos dois papéis: cache do `agno.Agent` por tupla de parâmetros + trace + retry.

    O Agent do agno recebe modelo/temperatura/histórico na construção, então mudar
    qualquer um deles no Studio exige reconstruir o objeto. A tupla é conferida a cada
    chamada (leitura de dicionário em memória, barata) e o Agent só é refeito quando muda.
    """

    papel = "llm"

    def __init__(
        self,
        model_id: str | None = None,
        db_path: Path | None = None,
        *,
        agent: Any | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        trace: Trace | None = None,
    ) -> None:
        self._model_id = model_id
        self._db_path = db_path
        self._sleep = sleep
        self._trace = trace
        self._agent_injetado = agent      # dublê nos testes: não carrega o SDK nem exige chave
        self._agent: Any | None = None
        self._chave_atual: tuple | None = None
        # Custo da última chamada de cada conversa, à espera de o turno drenar para o log.
        self._uso: dict[str, dict[str, Any]] = {}

    # ---- parâmetros do store
    def _modelo(self) -> str:
        return self._model_id or store.param("settings.gemini_model")

    def _db_file(self) -> Path:
        return Path(self._db_path or store.param("settings.agent_db_path"))

    def _chave(self) -> tuple:
        """Tupla que identifica o Agent construído. Mudou ⇒ reconstrói."""
        raise NotImplementedError

    def _construir(self) -> Any:
        raise NotImplementedError

    def agente(self) -> Any:
        """Agent atual, reconstruído se algum parâmetro do store mudou."""
        if self._agent_injetado is not None:
            return self._agent_injetado
        chave = self._chave()
        if self._agent is None or chave != self._chave_atual:
            self._agent = self._construir()
            self._chave_atual = chave
        return self._agent

    def _sqlite_db(self) -> Any:
        from agno.db.sqlite import SqliteDb

        db_file = self._db_file()
        db_file.parent.mkdir(parents=True, exist_ok=True)
        return SqliteDb(db_file=str(db_file))

    def _gemini(self, temperatura: float) -> Any:
        from agno.models.google import Gemini

        return Gemini(id=self._modelo(), api_key=settings.google_api_key, temperature=temperatura)

    # ---- retry / trace
    def _retry_kwargs(self) -> dict[str, Any]:
        return {
            "sleep": self._sleep,
            "max_tentativas": int(store.param("settings.llm_max_tentativas")),
            "budget_s": float(store.param("settings.llm_budget_s")),
            "papel": self.papel,
            "emitir": self.emitir_evento,
        }

    async def _arun(self, entrada: Any, **kwargs: Any) -> Any:
        """`arun` com teto de tempo por CHAMADA.

        O `_com_retry` só limitava as esperas entre tentativas; a chamada em si podia levar o
        que quisesse — a Fase 3 mediu 51 s e 70 s, com o lead em silêncio o tempo todo. O
        `TimeoutError` é transitório por `_e_transitorio`, então a tentativa seguinte acontece
        normalmente e, esgotado tudo, cai no fallback determinístico.
        """
        return await asyncio.wait_for(self.agente().arun(entrada, **kwargs), timeout=llm_timeout_s())

    def emitir_evento(self, campos: dict[str, Any]) -> None:
        """Canal de eventos do `_com_retry` (`llm_retry`, `llm_error`): o mesmo trace."""
        self.emitir_trace(**campos)

    def emitir_trace(self, **campos: Any) -> None:
        """Manda um evento de trace para quem injetou o hook. Nunca quebra o turno."""
        if self._trace is None:
            return
        campos.setdefault("evento", "llm_call")
        campos.setdefault("papel", self.papel)
        campos.setdefault("modelo", self._modelo())
        try:
            self._trace(campos)
        except Exception as exc:  # noqa: BLE001 — observabilidade não derruba conversa
            log.warning("trace falhou (%s): %s", type(exc).__name__, str(exc)[:120])

    # ---- custo por chamada
    def _guardar_uso(self, conversation_id: str, **campos: Any) -> None:
        """Guarda o custo da chamada para o turno drenar. Nunca cresce sem limite."""
        if len(self._uso) > 100:
            self._uso.clear()          # canal que nunca drena não vira vazamento
        self._uso[conversation_id] = {"model": self._modelo(), **campos}

    def drenar_usage(self, conversation_id: str) -> dict[str, Any]:
        """Entrega ao turno o custo da última chamada e esquece — quem loga é a Conversation.

        Formato pronto para virar `**kwargs` do evento `llm_call`:
        `{model, usage: {input, output, total, cache_read} | None, tentativas, source, guard}`.
        """
        return self._uso.pop(conversation_id, {})


class Extractor(_AgenteLLM):
    """Agent com `output_schema=Extraction`, sem histórico e sem tools.

    Sem histórico de propósito: cada mensagem é analisada isolada, e todo o contexto
    necessário (estado + última pergunta) já vai no prompt — assim a extração é
    reprodutível e não se contamina com turnos antigos.
    """

    papel = "extractor"

    def __init__(self, *args: Any, planos: dict | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # `/planos` do boot: é dele que saem os ids aceitos em `plano_id`.
        self._planos = planos

    def _chave(self) -> tuple:
        return (self._modelo(), float(store.param("settings.extractor_temperature")), str(self._db_file()))

    def _construir(self) -> Any:
        from agno.agent import Agent

        return Agent(
            name="autoseguro-extractor",
            model=self._gemini(float(store.param("settings.extractor_temperature"))),
            db=self._sqlite_db(),
            output_schema=Extraction,
            instructions=_extractor_instructions,
            add_history_to_context=False,
            markdown=False,
            telemetry=False,
        )

    async def extract(self, text: str, state: LeadState, today: date) -> Extraction:
        """Nunca levanta. Cota estourada / 5xx re-tenta; esgotou, marca `indisponivel`."""
        session_id = f"extract-{state.conversation_id}"
        instructions = build_extraction_instructions(state, today, planos=self._planos)
        tentativa = 0
        ultimo_erro: str | None = None
        usage: dict[str, int] | None = None

        async def chamada() -> Extraction:
            nonlocal tentativa, ultimo_erro, usage
            tentativa += 1
            inicio = time.perf_counter()
            try:
                run = await self._arun(
                    text,
                    session_id=session_id,
                    dependencies={"state": state, "today": today, "planos": self._planos},
                )
            except Exception as exc:
                ultimo_erro = str(exc)
                self.emitir_trace(
                    session_id=session_id, tentativa=tentativa, instructions=instructions, historico=[],
                    entrada=text, saida=None, status="erro", latency_ms=_ms(inicio), erro=str(exc)[:500],
                    usage=None,
                )
                raise
            latency_ms = _ms(inicio)
            historico = _historico_do_run(run)
            erro = _erro_do_run(run)
            usage_da_vez = _usage_do_run(run)
            usage = _somar_usage(usage, usage_da_vez)
            if erro is None and isinstance(run.content, Extraction):
                self.emitir_trace(
                    session_id=session_id, tentativa=tentativa, instructions=instructions, historico=historico,
                    entrada=text, saida=run.content.model_dump(mode="json"), status="ok",
                    latency_ms=latency_ms, erro=None, usage=usage_da_vez,
                )
                return run.content
            # Sem status de erro e fora do schema: o modelo respondeu outra coisa.
            # Não é transitório — re-tentar só queimaria cota.
            if erro is None:
                erro = f"conteúdo fora do schema: {type(run.content).__name__}"
                status_code: int | None = 422
            else:
                status_code = None
            ultimo_erro = erro
            self.emitir_trace(
                session_id=session_id, tentativa=tentativa, instructions=instructions, historico=historico,
                entrada=text, saida=run.content if isinstance(run.content, str) else None, status="erro",
                latency_ms=latency_ms, erro=erro[:500], usage=usage_da_vez,
            )
            raise ChamadaLLMFalhou(erro, status_code=status_code)

        try:
            extraida = await _com_retry(chamada, **self._retry_kwargs())
        except Exception as exc:  # noqa: BLE001 — o turno continua sem extração
            log.warning("extractor indisponível (%s): %s", type(exc).__name__, str(exc)[:200])
        else:
            self._guardar_uso(state.conversation_id, usage=usage, tentativas=tentativa, source="llm")
            return extraida
        degradada = Extraction(intent=Intent.OUTRO, indisponivel=True, observacao="extracao_indisponivel")
        self.emitir_trace(
            session_id=session_id, tentativa=tentativa, instructions=instructions, historico=[],
            entrada=text, saida=degradada.model_dump(mode="json"), status="fallback",
            latency_ms=0, erro=(ultimo_erro or "")[:500] or None, usage=None,
        )
        self._guardar_uso(state.conversation_id, usage=usage, tentativas=tentativa, source="fallback")
        return degradada


class Responder(_AgenteLLM):
    """Agent conversacional: sem output_schema, com histórico por `session_id` e as tools do painel."""

    papel = "responder"

    def __init__(self, *args: Any, planos: dict | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # `/planos` do boot: é dele que sai a lista de coberturas dos guardrails.
        self._planos = planos
        # `tool_call`s do último `reply` de cada conversa, à espera do turno drenar para o log.
        self._tool_calls: dict[str, list[dict[str, Any]]] = {}

    def _chave(self) -> tuple:
        return (
            self._modelo(),
            float(store.param("settings.responder_temperature")),
            responder_history_runs(),
            str(self._db_file()),
            store.custom_tools_version(),   # tool criada/editada/desligada ⇒ Agent novo
        )

    def _construir(self) -> Any:
        from agno.agent import Agent

        from agent.tools_runtime import carregar_tools

        kwargs: dict[str, Any] = {
            "name": "autoseguro-responder",
            "model": self._gemini(float(store.param("settings.responder_temperature"))),
            "db": self._sqlite_db(),
            "instructions": _responder_instructions,
            "post_hooks": [_guard_hook],
            "markdown": False,
            "telemetry": False,
            **historico_kwargs(),
        }
        # Sem tool habilitada o kwarg `tools` nem existe: o Agent é byte a byte o da entrega.
        tools = carregar_tools(store, self._registrar_tool_call)
        if tools:
            kwargs["tools"] = tools
        return Agent(**kwargs)

    # ---- tool calls do turno
    def _registrar_tool_call(self, evento: dict[str, Any]) -> None:
        chamadas = _TOOL_CALLS_DO_TURNO.get()
        if chamadas is not None:
            chamadas.append(evento)

    def drenar_tool_calls(self, conversation_id: str) -> list[dict[str, Any]]:
        """Entrega ao turno os `tool_call` da última resposta e esquece — quem loga é a Conversation."""
        return self._tool_calls.pop(conversation_id, [])

    def abertura(self, directive: str, state: LeadState) -> str | None:
        """Texto da abertura, quando o turno 1 é exatamente "apresente-se e pergunte a idade".

        O primeiro turno de quase toda conversa é esse, e ele não precisa de modelo: virou
        template (slot `responder.abertura`) e economiza uma chamada inteira por conversa.
        Qualquer outra forma de turno 1 (o lead já mandou a idade, pediu humano, fez uma
        pergunta) continua no LLM, com a diretiva de abertura prefixada.
        """
        if state.turnos > 1 or state.idade is not None:
            return None
        if directive != store.text("diretiva.idade"):
            return None
        return store.text("responder.abertura")

    async def reply(
        self,
        directive: str,
        state: LeadState,
        inbound_text: str,
        *,
        on_slow: Callable[[], Awaitable[None]] | None = None,
    ) -> RespostaLLM:
        """Nunca devolve vazio: LLM fora do ar cai no fallback determinístico do campo.

        `on_slow` é chamado no MÁXIMO uma vez por turno, quando a primeira chamada estoura o
        `llm_timeout_s` — é o "só um instante" que faltava nas chamadas de 51 s e 70 s.
        """
        session_id = state.conversation_id
        pronta = self.abertura(directive, state)
        if pronta is not None:
            self._guardar_uso(session_id, usage=None, tentativas=0, source="template")
            return RespostaLLM(pronta, source="template")

        instructions = build_responder_instructions(state, directive, self._planos)
        entrada = inbound_text or "(sem texto)"
        tentativa = 0
        ultimo_erro: str | None = None
        usage: dict[str, int] | None = None
        guard: dict[str, str] | None = None
        avisado = False

        def registrar_guard(achado: dict[str, str]) -> None:
            nonlocal guard
            guard = achado
            self.emitir_trace(evento="llm_guard", session_id=session_id, **achado)

        async def chamada() -> str:
            nonlocal tentativa, ultimo_erro, usage, avisado
            tentativa += 1
            inicio = time.perf_counter()
            try:
                run = await self._arun(
                    entrada,
                    session_id=session_id,
                    dependencies={"state": state, "directive": directive, "planos": self._planos},
                )
            except Exception as exc:
                ultimo_erro = str(exc)
                self.emitir_trace(
                    session_id=session_id, tentativa=tentativa, instructions=instructions, historico=[],
                    entrada=entrada, saida=None, status="erro", latency_ms=_ms(inicio), erro=str(exc)[:500],
                    usage=None,
                )
                if isinstance(exc, TimeoutError) and on_slow is not None and not avisado:
                    avisado = True                 # uma vez por turno, por mais retries que venham
                    await on_slow()
                raise
            latency_ms = _ms(inicio)
            historico = _historico_do_run(run)
            erro = _erro_do_run(run)
            usage_da_vez = _usage_do_run(run)
            usage = _somar_usage(usage, usage_da_vez)
            texto = run.content.strip() if erro is None and isinstance(run.content, str) else ""
            if erro is None and texto:
                self.emitir_trace(
                    session_id=session_id, tentativa=tentativa, instructions=instructions, historico=historico,
                    entrada=entrada, saida=texto, status="ok", latency_ms=latency_ms, erro=None,
                    usage=usage_da_vez, system_do_historico=len(system_do_historico(historico)),
                )
                return texto
            if erro is None:
                erro = "resposta vazia do modelo"
                status_code: int | None = 422
            else:
                status_code = None
            ultimo_erro = erro
            self.emitir_trace(
                session_id=session_id, tentativa=tentativa, instructions=instructions, historico=historico,
                entrada=entrada, saida=None, status="erro", latency_ms=latency_ms, erro=erro[:500],
                usage=usage_da_vez,
            )
            raise ChamadaLLMFalhou(erro, status_code=status_code)

        chamadas: list[dict[str, Any]] = []
        achados_do_hook: list[dict[str, str]] = []
        token = _TOOL_CALLS_DO_TURNO.set(chamadas)
        token_guard = _GUARDS_DO_TURNO.set(achados_do_hook)
        try:
            texto = await _com_retry(chamada, **self._retry_kwargs())
        except Exception as exc:  # noqa: BLE001 — o lead não pode ficar sem resposta
            log.warning("responder indisponível (%s): %s", type(exc).__name__, str(exc)[:200])
            degradado = _fallback(state)
            self.emitir_trace(
                session_id=session_id, tentativa=tentativa, instructions=instructions, historico=[],
                entrada=entrada, saida=degradado, status="fallback", latency_ms=0,
                erro=(ultimo_erro or "")[:500] or None, usage=None,
            )
            self._guardar_uso(
                session_id, usage=usage, tentativas=tentativa, source="fallback", guard=guard
            )
            return RespostaLLM(degradado, source="fallback")
        finally:
            _TOOL_CALLS_DO_TURNO.reset(token)
            _GUARDS_DO_TURNO.reset(token_guard)
            for achado_do_hook in achados_do_hook:
                registrar_guard(achado_do_hook)     # o hook já trocou o texto; aqui só reporta
            if chamadas:
                if len(self._tool_calls) > 100:
                    self._tool_calls.clear()   # canal que nunca drena não vira vazamento
                self._tool_calls[session_id] = chamadas

        # De novo fora do hook: o agno engole exceção de post_hook, então a regra não pode
        # depender dele. Se o hook já trocou o texto, aqui não sobra violação nenhuma.
        final, achado = guard_resposta(texto, state, directive)
        if achado is not None:
            registrar_guard(achado)
        source = "llm" if guard is None else "fallback"
        self._guardar_uso(session_id, usage=usage, tentativas=tentativa, source=source, guard=guard)
        return RespostaLLM(final, source=source)


def _ms(inicio: float) -> int:
    return int((time.perf_counter() - inicio) * 1000)
