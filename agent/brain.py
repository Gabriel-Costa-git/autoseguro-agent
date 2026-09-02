"""Camada LLM do agente: `Extractor` (mensagem → `Extraction`) e `Responder` (diretiva → texto).

O LLM faz só duas coisas: extrair dados e falar. Ele não decide nada (isso é da
`policy`) e nunca vê preço — o estado enviado no prompt é resumido sem valores e
`guard_price` é a última linha de defesa da regra de ouro: preço só sai da API,
renderizado pelo `presenter`.

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
from typing import Any

from agent.config import settings
from agent.models import CampoColeta, Extraction, Intent, LeadState, QuoteOutcome
from agent.runtime_config import store

log = logging.getLogger("autoseguro.brain")

Trace = Callable[[dict[str, Any]], None]

# Campos com slot próprio de fallback/diretiva; qualquer outro cai no texto padrão.
CAMPOS: tuple[str, ...] = ("idade", "veiculo", "cep", "plano", "data_inicio")

# Quem executa a tool é o agno, lá dentro do `arun`, com uma `Function` que foi construída
# junto com o Agent (cacheado e compartilhado entre conversas) — ela não sabe de qual turno
# veio a chamada. O vínculo é este ContextVar: `reply` publica a lista do turno antes de
# chamar o modelo, e cada execução de tool anota nela. Como cada turno roda na sua própria
# task asyncio, dois leads simultâneos não se misturam.
_TOOL_CALLS_DO_TURNO: ContextVar[list[dict[str, Any]] | None] = ContextVar("tool_calls_do_turno", default=None)

# --------------------------------------------------------------------------- guardrail
# Só dinheiro: "R$", "209,90" e "reais". Percentual fica fora porque o lead pode
# falar de franquia/desconto sem que a resposta cite valor — o prompt já proíbe.
_PRECO_RE = re.compile(r"R\$|\d+,\d{2}|\bre[aá]is\b", re.IGNORECASE)


def fallback_text(campo: str | None) -> str:
    """Texto determinístico do campo pendente (slot `fallback.<campo>`)."""
    return store.text(f"fallback.{campo}") if campo in CAMPOS else store.text("fallback.padrao")


def contem_preco(texto: str) -> bool:
    """True se o texto tem cara de valor em dinheiro (R$, 1.234,56, 'reais')."""
    return bool(_PRECO_RE.search(texto or ""))


def guard_price(text: str, state: LeadState) -> str:
    """Substitui a resposta do LLM por um fallback determinístico se ela citar dinheiro.

    Só libera valor quando ele veio da API (`quote_result.outcome == OK`); nesse caso
    o texto com preço é o do `presenter`, não do LLM, e o Responder está apenas
    conversando em cima de uma cotação já apresentada.

    Sem toggle e sem parâmetro: este é o guardrail da regra de ouro, não é
    configurável pelo Studio.
    """
    cotado = state.quote_result is not None and state.quote_result.outcome is QuoteOutcome.OK
    if cotado or not contem_preco(text):
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
) -> T:
    """Repete `fn` enquanto a falha for transitória. Levanta a última exceção ao esgotar.

    Respeita o `retryDelay` do provedor quando ele existe (é o tempo que ele mesmo
    diz que a cota leva para liberar); senão, backoff 2 s → 4 s → 8 s.
    """
    inicio = clock()
    tentativa = 1
    while True:
        try:
            return await fn()
        except Exception as exc:
            if tentativa >= max_tentativas or not _e_transitorio(exc):
                raise
            espera = _retry_delay(exc)
            if espera is None:
                espera = _BACKOFF_S[min(tentativa, len(_BACKOFF_S)) - 1]
            if clock() - inicio + espera > budget_s:
                raise
            log.warning(
                "%s: falha transitória (%s) na tentativa %d/%d; aguardando %.1fs",
                papel,
                _status_do_erro(exc) or type(exc).__name__,
                tentativa,
                max_tentativas,
                espera,
            )
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


# --------------------------------------------------------------------------- prompts
def _intent_exemplos() -> dict[Intent, str]:
    """Exemplos por intent, na ordem do enum (slots `intent.<valor>`)."""
    return {i: store.text(f"intent.{i.value}") for i in Intent}


def directive_for_field(campo: CampoColeta, motivo: str | None = None) -> str:
    """Traduz um `AskField` da policy na diretiva em linguagem natural do Responder."""
    base = store.text(f"diretiva.{campo}") if campo in CAMPOS else f"pergunte {campo}"
    return f"{base} (contexto: {motivo})" if motivo else base


def resumo_state(state: LeadState) -> str:
    """Resumo do estado para o prompt. NUNCA inclui valores da cotação — só o status."""
    partes = [
        f"idade: {state.idade if state.idade is not None else '—'}",
        f"carro: {state.veiculo_texto or '—'} (ano {state.veiculo_ano or '—'})",
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


def build_extraction_instructions(state: LeadState, today: date) -> str:
    """Prompt do Extractor (slot `extractor.instructions`): data, estado, última pergunta, intents."""
    intents = "\n".join(f"- {i.value}: {ex}" for i, ex in _intent_exemplos().items())
    return store.text(
        "extractor.instructions",
        today=today.isoformat(),
        ano=today.year,
        resumo=resumo_state(state),
        ultima=state.ultima_pergunta or "nenhuma",
        intents=intents,
    )


def build_responder_instructions(state: LeadState, directive: str) -> str:
    """Prompt do Responder (slot `responder.instructions`): persona, estado e diretiva do turno."""
    return store.text("responder.instructions", resumo=resumo_state(state), diretiva=directive)


# --------------------------------------------------------------------------- agentes agno
def _price_guard_hook(run_output: Any, run_context: Any) -> None:
    """post_hook do agno: sanitiza a resposta ANTES dela entrar no histórico da sessão.

    Sem isso, uma resposta com preço ficaria gravada no histórico e contaminaria os
    turnos seguintes. Como o agno engole exceções de hook, o `Responder.reply`
    reaplica `guard_price` na saída — a garantia não pode depender do hook.
    """
    deps = getattr(run_context, "dependencies", None) or {}
    state = deps.get("state")
    if state is None or not isinstance(run_output.content, str):
        return
    run_output.content = guard_price(run_output.content, state)


def _extractor_instructions(run_context: Any) -> str:
    deps = getattr(run_context, "dependencies", None) or {}
    return build_extraction_instructions(deps["state"], deps["today"])


def _responder_instructions(run_context: Any) -> str:
    deps = getattr(run_context, "dependencies", None) or {}
    return build_responder_instructions(deps["state"], deps["directive"])


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
        }

    def emitir_trace(self, **campos: Any) -> None:
        """Manda um evento de trace para quem injetou o hook. Nunca quebra o turno."""
        if self._trace is None:
            return
        campos.setdefault("papel", self.papel)
        campos.setdefault("modelo", self._modelo())
        try:
            self._trace(campos)
        except Exception as exc:  # noqa: BLE001 — observabilidade não derruba conversa
            log.warning("trace falhou (%s): %s", type(exc).__name__, str(exc)[:120])


class Extractor(_AgenteLLM):
    """Agent com `output_schema=Extraction`, sem histórico e sem tools.

    Sem histórico de propósito: cada mensagem é analisada isolada, e todo o contexto
    necessário (estado + última pergunta) já vai no prompt — assim a extração é
    reprodutível e não se contamina com turnos antigos.
    """

    papel = "extractor"

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
        instructions = build_extraction_instructions(state, today)
        tentativa = 0
        ultimo_erro: str | None = None

        async def chamada() -> Extraction:
            nonlocal tentativa, ultimo_erro
            tentativa += 1
            inicio = time.perf_counter()
            try:
                run = await self.agente().arun(
                    text,
                    session_id=session_id,
                    dependencies={"state": state, "today": today},
                )
            except Exception as exc:
                ultimo_erro = str(exc)
                self.emitir_trace(
                    session_id=session_id, tentativa=tentativa, instructions=instructions, historico=[],
                    entrada=text, saida=None, status="erro", latency_ms=_ms(inicio), erro=str(exc)[:500],
                )
                raise
            latency_ms = _ms(inicio)
            historico = _historico_do_run(run)
            erro = _erro_do_run(run)
            if erro is None and isinstance(run.content, Extraction):
                self.emitir_trace(
                    session_id=session_id, tentativa=tentativa, instructions=instructions, historico=historico,
                    entrada=text, saida=run.content.model_dump(mode="json"), status="ok",
                    latency_ms=latency_ms, erro=None,
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
                latency_ms=latency_ms, erro=erro[:500],
            )
            raise ChamadaLLMFalhou(erro, status_code=status_code)

        try:
            return await _com_retry(chamada, **self._retry_kwargs())
        except Exception as exc:  # noqa: BLE001 — o turno continua sem extração
            log.warning("extractor indisponível (%s): %s", type(exc).__name__, str(exc)[:200])
        degradada = Extraction(intent=Intent.OUTRO, indisponivel=True, observacao="extracao_indisponivel")
        self.emitir_trace(
            session_id=session_id, tentativa=tentativa, instructions=instructions, historico=[],
            entrada=text, saida=degradada.model_dump(mode="json"), status="fallback",
            latency_ms=0, erro=(ultimo_erro or "")[:500] or None,
        )
        return degradada


class Responder(_AgenteLLM):
    """Agent conversacional: sem output_schema, com histórico por `session_id` e as tools do painel."""

    papel = "responder"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # `tool_call`s do último `reply` de cada conversa, à espera do turno drenar para o log.
        self._tool_calls: dict[str, list[dict[str, Any]]] = {}

    def _chave(self) -> tuple:
        return (
            self._modelo(),
            float(store.param("settings.responder_temperature")),
            int(store.param("settings.responder_history_runs")),
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
            "post_hooks": [_price_guard_hook],
            "add_history_to_context": True,
            "num_history_runs": int(store.param("settings.responder_history_runs")),
            "markdown": False,
            "telemetry": False,
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

    async def reply(self, directive: str, state: LeadState, inbound_text: str) -> str:
        """Nunca devolve vazio: LLM fora do ar cai no fallback determinístico do campo."""
        session_id = state.conversation_id
        instructions = build_responder_instructions(state, directive)
        entrada = inbound_text or "(sem texto)"
        tentativa = 0
        ultimo_erro: str | None = None

        async def chamada() -> str:
            nonlocal tentativa, ultimo_erro
            tentativa += 1
            inicio = time.perf_counter()
            try:
                run = await self.agente().arun(
                    entrada,
                    session_id=session_id,
                    dependencies={"state": state, "directive": directive},
                )
            except Exception as exc:
                ultimo_erro = str(exc)
                self.emitir_trace(
                    session_id=session_id, tentativa=tentativa, instructions=instructions, historico=[],
                    entrada=entrada, saida=None, status="erro", latency_ms=_ms(inicio), erro=str(exc)[:500],
                )
                raise
            latency_ms = _ms(inicio)
            historico = _historico_do_run(run)
            erro = _erro_do_run(run)
            texto = run.content.strip() if erro is None and isinstance(run.content, str) else ""
            if erro is None and texto:
                self.emitir_trace(
                    session_id=session_id, tentativa=tentativa, instructions=instructions, historico=historico,
                    entrada=entrada, saida=texto, status="ok", latency_ms=latency_ms, erro=None,
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
            )
            raise ChamadaLLMFalhou(erro, status_code=status_code)

        chamadas: list[dict[str, Any]] = []
        token = _TOOL_CALLS_DO_TURNO.set(chamadas)
        try:
            texto = await _com_retry(chamada, **self._retry_kwargs())
        except Exception as exc:  # noqa: BLE001 — o lead não pode ficar sem resposta
            log.warning("responder indisponível (%s): %s", type(exc).__name__, str(exc)[:200])
            degradado = _fallback(state)
            self.emitir_trace(
                session_id=session_id, tentativa=tentativa, instructions=instructions, historico=[],
                entrada=entrada, saida=degradado, status="fallback", latency_ms=0,
                erro=(ultimo_erro or "")[:500] or None,
            )
            return degradado
        finally:
            _TOOL_CALLS_DO_TURNO.reset(token)
            if chamadas:
                if len(self._tool_calls) > 100:
                    self._tool_calls.clear()   # canal que nunca drena não vira vazamento
                self._tool_calls[session_id] = chamadas
        return guard_price(texto, state)


def _ms(inicio: float) -> int:
    return int((time.perf_counter() - inicio) * 1000)
