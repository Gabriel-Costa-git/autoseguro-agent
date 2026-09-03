"""Orquestração de um turno: entrada do canal → extração → policy → ações → saídas.

Este módulo não decide nada e não formata preço: ele executa as `Action`s que a
`policy` devolveu, chamando o LLM só onde a ação pede texto livre (`AskField`,
`Reply`) e o `presenter` no resto. Toda decisão e todo efeito viram evento no log
JSONL da conversa, que é o rastro auditável da entrega.

`policy`, `presenter`, `cep` e o logger entram por injeção (com o módulo real como
padrão) para o turno ser testável sem rede, sem LLM e sem depender da ordem em que
os módulos irmãos ficam prontos.

Três economias de LLM, todas com o mesmo critério — só onde a resposta é
determinística e o modelo não acrescenta nada:

1. **Short-circuit do handoff**: conversa já com um consultor não passa pelo
   Extractor. Nada do que o lead escrever muda a decisão, e foi por aqui que 23
   eventos de protocolo do WhatsApp viraram 23 chamadas e 23 mensagens.
2. **Pré-parser**: "sim", um CEP e o nome de um plano, quando são a resposta
   inteira à pergunta que acabou de sair, viram `Extraction` por regex.
3. **Template-first**: pergunta de campo sem contexto, fora do primeiro turno, é
   o texto fixo do slot — o Responder só entra quando há o que explicar.

E uma garantia no fim do turno: se nada saiu e o estado não é um terminal já
avisado, o lead recebe um texto neutro e o log ganha um `error turno_silencioso`.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from agent.brain import directive_for_field, fallback_text
from agent.defaults import SLOTS
from agent.models import (
    Action,
    CepInfo,
    Extraction,
    Handoff,
    HandoffReason,
    Inbound,
    Intent,
    LeadState,
    Outbound,
    SendText,
    Stage,
)

# `config_store` (e não `store`) porque neste módulo `store` já é o store de ESTADO da conversa.
from agent.runtime_config import store as config_store

log = logging.getLogger("autoseguro.conversation")

# Compatibilidade: valem o texto ENTREGUE (versão `default`). O texto efetivo, editável
# no Studio, vem de `config_store.text(...)` na hora de enviar.
TEXTO_ERRO = SLOTS["conversation.texto_erro"]["default"]
TEXTO_LENTO = SLOTS["conversation.texto_lento"]["default"]

# Uma rodada extra por efeito (cotação, lookup de CEP) já cobre o fluxo; o limite
# existe só para nenhum bug de policy virar laço infinito de mensagens.
MAX_RODADAS = 3

# --------------------------------------------------------------------------- pré-parser
# Respostas de uma palavra à pergunta que acabou de sair. Só valem quando a mensagem é ISSO e
# mais nada: com "?", vírgula, "e" ou "mas" a frase tem outra intenção junto e o LLM decide.
_SIM = frozenset({"sim", "isso", "isso mesmo", "correto", "certo", "ok", "claro", "confirmo", "pode ser", "é", "eh", "sim!"})
_NAO = frozenset({"não", "nao", "errado", "não é", "nao e", "negativo", "não!", "nao!"})
_SO_CEP_RE = re.compile(r"^[\d\s.\-]+$")
_COMPOSTA_RE = re.compile(r"[?,;]|\b(e|mas|ou|porém|porem)\b", re.IGNORECASE)
# Só a idade por regex: "35", "35 anos", "tenho 35 anos". A faixa 16-110 é de propósito estreita —
# fora dela (um ano de fabricação, um CEP truncado) o modelo decide, que é o que ele sabe fazer.
_SO_IDADE_RE = re.compile(r"^(?:tenho\s+|eu\s+tenho\s+)?(\d{1,3})(?:\s*anos?)?$", re.IGNORECASE)
IDADE_MIN_PRE_PARSER, IDADE_MAX_PRE_PARSER = 16, 110


def policy_terminais() -> frozenset[Stage]:
    """Etapas terminais, lidas da policy (import tardio: o módulo entra por injeção nos testes)."""
    from agent.policy import STAGES_TERMINAIS

    return STAGES_TERMINAIS


def _sinal_terminal() -> Extraction:
    from agent.policy import TERMINAL_SEM_EXTRACAO

    return TERMINAL_SEM_EXTRACAO


def _payload_de_erro(state: LeadState) -> dict[str, Any]:
    """Payload do handoff de erro interno: o MESMO da policy (dados + cotações + motivo).

    O consultor que recebe um turno quebrado precisa dos dados tanto quanto o que recebe um
    handoff normal; o payload de três chaves de antes obrigava a garimpar o JSONL.
    """
    try:
        from agent.policy import _payload_handoff

        return _payload_handoff(state, HandoffReason.ERRO_INTERNO)
    except Exception:  # noqa: BLE001 — o handoff não pode falhar por causa do payload
        return {"conversation_id": state.conversation_id, "motivo": "erro_interno"}


class InMemoryStateStore:
    """Store de estado em memória (o CLI usa; um canal real trocaria por Redis/DB)."""

    def __init__(self) -> None:
        self._estados: dict[str, LeadState] = {}

    def get(self, conversation_id: str) -> LeadState | None:
        estado = self._estados.get(conversation_id)
        return estado.model_copy(deep=True) if estado else None

    def put(self, state: LeadState) -> None:
        self._estados[state.conversation_id] = state.model_copy(deep=True)


@dataclass
class _Turno:
    """Contexto vivo de um turno (destinatário das saídas e do log)."""

    inbound: Inbound
    emit: Callable[[Outbound], Awaitable[None]]
    logger: Any
    today: date
    saidas: int = field(default=0)


def _usage(agente: object, conversation_id: str) -> dict:
    """`model`, `usage` e `tentativas` da última chamada, se o agente souber drenar; senão nada."""
    drenar = getattr(agente, "drenar_usage", None)
    if drenar is None:
        return {}
    try:
        return drenar(conversation_id) or {}
    except Exception:  # noqa: BLE001 — observabilidade não derruba o turno
        return {}


class Conversation:
    def __init__(
        self,
        rules: Any,
        quote_client: Any,
        extractor: Any,
        responder: Any,
        log_dir: Path,
        store: Any,
        today: Callable[[], date] = date.today,
        *,
        next_action: Callable[..., tuple[LeadState, list[Action]]] | None = None,
        render: Callable[[Action, LeadState], str] | None = None,
        lookup_cep: Callable[..., Awaitable[Any]] | None = None,
        logger_factory: Callable[[Path, str], Any] | None = None,
        cep_timeout_s: float | None = None,
        on_handoff: Callable[[LeadState, Any], Awaitable[None]] | None = None,
        rules_provider: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self.rules = rules
        self.quote_client = quote_client
        self.extractor = extractor
        self.responder = responder
        self.log_dir = Path(log_dir)
        self.store = store
        self._today = today
        self._cep_timeout_s = cep_timeout_s  # None = usa o valor efetivo do store na chamada
        self._on_handoff = on_handoff        # avisa o consultor; None = ninguém é avisado (CLI)
        # Relê o `/planos` (com TTL) e recalcula as `Rules` com o `today()` de AGORA. Sem ele, as
        # regras são as do boot: um processo que vira o ano continua recusando pelo ano velho.
        self._rules_provider = rules_provider

        if next_action is None:
            from agent.policy import next_action as next_action_real

            next_action = next_action_real
        if render is None:
            from agent.presenter import render as render_real

            render = render_real
        if lookup_cep is None:
            from agent.cep import lookup_cep as lookup_cep_real

            lookup_cep = lookup_cep_real
        if logger_factory is None:
            from agent.observability import ConversationLogger

            logger_factory = ConversationLogger
        self._next_action = next_action
        self._render = render
        self._lookup_cep = lookup_cep
        self._logger_factory = logger_factory

    # ------------------------------------------------------------------ turno
    async def handle(self, inbound: Inbound, emit: Callable[[Outbound], Awaitable[None]]) -> LeadState:
        """Processa uma mensagem do lead. Nunca levanta e nunca deixa o lead sem resposta."""
        state = self.store.get(inbound.conversation_id) or LeadState(conversation_id=inbound.conversation_id)
        if inbound.sender_name and not state.lead_nome:
            state.lead_nome = inbound.sender_name.split()[0]
        # A origem é do primeiro contato: o canal que abriu a conversa manda, e uma mensagem
        # sem origem (canal antigo, replay) não apaga a que já está no estado.
        state.origem = state.origem or inbound.origem
        turno = _Turno(
            inbound=inbound,
            emit=emit,
            logger=self._logger_factory(self.log_dir, inbound.conversation_id),
            today=self._today(),
        )
        turno.logger.event(
            "inbound",
            message_id=inbound.message_id,
            text=inbound.text,
            media_type=inbound.media_type,
            sender_name=inbound.sender_name,
            origem=state.origem,
        )
        try:
            extraction = await self._extrair(state, turno)
            # A vitrine de planos e a cotação usam as regras: relê o catálogo ANTES de decidir
            # quando ele ainda pode entrar na mensagem deste turno.
            if state.plano_id is None and state.stage not in policy_terminais():
                await self._atualizar_rules(turno)
            state = await self._decidir_e_executar(state, extraction, turno, rodada=0)
            state = await self._garantir_resposta(state, turno)
        except Exception as exc:  # noqa: BLE001 — o lead não pode ficar no vácuo
            state = await self._falhar(state, turno, exc)
        self.store.put(state)
        return state

    async def _garantir_resposta(self, state: LeadState, turno: _Turno) -> LeadState:
        """Invariante do turno: ninguém fica sem resposta — exceto o terminal já avisado.

        Silêncio é o único desfecho que o lead não consegue interpretar. Quando acontece, é bug
        de policy, então o log ganha um `error` com o estado para o achado ser rastreável.
        """
        if turno.saidas:
            return state
        if state.stage in policy_terminais() and state.terminal_avisado:
            return state          # handoff já respondido: silêncio é a decisão, não um esquecimento
        turno.logger.event(
            "error",
            message_id=turno.inbound.message_id,
            erro="turno_silencioso",
            detalhe=f"stage={state.stage.value}",
        )
        await self._enviar(config_store.text("conversation.texto_erro"), "template", turno)
        return state

    async def _atualizar_rules(self, turno: _Turno) -> None:
        """Recalcula `self.rules` pelo provider e registra o `planos_refresh` do turno."""
        if self._rules_provider is None:
            return
        anterior = self.rules
        erro: str | None = None
        try:
            self.rules = await self._rules_provider()
        except Exception as exc:  # noqa: BLE001 — planos velhos valem mais que turno derrubado
            erro = f"{type(exc).__name__}: {exc}"[:200]
            log.error("não consegui reler o /planos: %s", erro)
        snapshot = getattr(self.quote_client, "ultimo_planos", None)
        dados: dict[str, Any] = {
            "mudou": self.rules is not anterior,
            "origem": getattr(snapshot, "origem", "cache"),
            "idade_min": getattr(self.rules, "idade_min", None),
            "idade_max": getattr(self.rules, "idade_max", None),
            "ano_min": getattr(self.rules, "ano_min", None),
            "ano_max": getattr(self.rules, "ano_max", None),
            "planos": [p.id for p in self.rules.planos_resumo()] if hasattr(self.rules, "planos_resumo") else [],
            "latency_ms": int(getattr(snapshot, "latency_ms", 0) or 0),
            "idade_catalogo_s": round(float(getattr(snapshot, "idade_s", 0.0) or 0.0), 3),
        }
        erro = erro or getattr(snapshot, "erro", None)
        if erro:
            dados["erro"] = erro
        turno.logger.event("planos_refresh", message_id=turno.inbound.message_id, **dados)

    async def _extrair(self, state: LeadState, turno: _Turno) -> Extraction | None:
        """Mídia sem texto não passa pelo LLM: `None` sinaliza isso para a policy."""
        inbound = turno.inbound
        if inbound.media_type != "text" or not (inbound.text or "").strip():
            return None

        # Conversa já com um consultor: nada que o lead escreva muda a decisão. O sinal é próprio
        # (e não `None`, que quer dizer mídia) para a policy saber que houve texto.
        if state.stage is Stage.HANDOFF:
            sinal = _sinal_terminal()
            turno.logger.event(
                "extraction", message_id=inbound.message_id, source="short_circuit",
                **sinal.model_dump(mode="json"),
            )
            return sinal

        regex = self._pre_parse(inbound.text or "", state)
        if regex is not None:
            turno.logger.event(
                "extraction", message_id=inbound.message_id, source="regex",
                **regex.model_dump(mode="json"),
            )
            return regex

        inicio = time.perf_counter()
        extraction = await self.extractor.extract(inbound.text or "", state, turno.today)
        turno.logger.event(
            "llm_call",
            message_id=inbound.message_id,
            papel="extractor",
            **_usage(self.extractor, state.conversation_id),
            latency_ms=int((time.perf_counter() - inicio) * 1000),
        )
        extraction, bruto = self._normalizar_consulta(extraction)
        dados = extraction.model_dump(mode="json")
        dados["source"] = "llm"
        if bruto is not None:
            dados["intent_bruto"] = bruto      # o modelo pediu consulta; sem ferramenta, não dá
        turno.logger.event("extraction", message_id=inbound.message_id, **dados)
        return extraction

    def _pre_parse(self, texto: str, state: LeadState) -> Extraction | None:
        """Resposta de uma palavra à pergunta pendente, sem LLM. `None` = manda para o modelo.

        Quatro casos, e só quando a mensagem é a resposta INTEIRA: sim/não na confirmação do
        CEP, um CEP onde o CEP foi pedido, o nome de um plano onde o plano foi pedido e a idade
        onde a idade foi pedida. Qualquer sinal de frase composta ("?", vírgula, "e", "mas")
        devolve `None` — nesses o modelo acerta e a gente não.

        O carro ("Onix 2022") ficou de FORA de propósito: separar modelo de ano por regex erra
        em nome com número (Fiat 500, Golf GTI 2020), em "o mesmo de antes" e em "Onix 2022 da
        minha mãe" — e o ano errado é preço errado. Está no backlog, com o motivo.
        """
        pergunta = state.ultima_pergunta
        if pergunta is None:
            return None
        limpo = texto.strip()
        if not limpo or len(limpo) > 40 or _COMPOSTA_RE.search(limpo):
            return None
        baixo = limpo.lower().rstrip(".!")

        if pergunta == "cep" and state.stage is Stage.CONFIRMA_CEP:
            if baixo in _SIM:
                return Extraction(intent=Intent.CONFIRMAR)
            if baixo in _NAO:
                return Extraction(intent=Intent.NEGAR)
            return None

        if pergunta == "cep" and _SO_CEP_RE.match(limpo):
            cep8 = self.rules.normalize_cep(limpo) if hasattr(self.rules, "normalize_cep") else None
            if cep8 is not None:
                return Extraction(intent=Intent.FORNECER_DADOS, cep=cep8)
            return None

        if pergunta == "plano" and baixo in self._ids_de_plano():
            return Extraction(intent=Intent.ESCOLHER_PLANO, plano_id=baixo)   # type: ignore[arg-type]

        if pergunta == "idade":
            achou = _SO_IDADE_RE.match(baixo)
            if achou is not None:
                idade = int(achou.group(1))
                if IDADE_MIN_PRE_PARSER <= idade <= IDADE_MAX_PRE_PARSER:
                    return Extraction(intent=Intent.FORNECER_DADOS, idade=idade)
            return None
        return None

    def _ids_de_plano(self) -> frozenset[str]:
        """Ids que o pré-parser aceita: os do catálogo corrente ∩ os que o `Extraction` permite."""
        from typing import get_args

        from agent.models import PlanoId

        try:
            correntes = {p.id for p in self.rules.planos_resumo()}
        except Exception:  # noqa: BLE001 — sem catálogo, o pré-parser simplesmente não age
            return frozenset()
        return frozenset(correntes & set(get_args(PlanoId)))

    def _normalizar_consulta(self, extraction: Extraction) -> tuple[Extraction, str | None]:
        """`consulta` sem tool habilitada vira `outro`.

        O valor está no enum, logo no schema do `Extraction`, e o modelo pode escolhê-lo mesmo
        num agente sem ferramenta nenhuma. Normalizar aqui mantém a policy alheia às tools — e o
        comportamento entregue idêntico. O intent original fica no log, para não sumir o sinal.
        """
        if extraction.intent is not Intent.CONSULTA:
            return extraction, None
        if config_store.custom_tools_habilitadas():
            return extraction, None
        return extraction.model_copy(update={"intent": Intent.OUTRO}), Intent.CONSULTA.value

    async def _decidir_e_executar(
        self, state: LeadState, extraction: Extraction | None, turno: _Turno, rodada: int
    ) -> LeadState:
        state, actions = self._next_action(state, extraction, self.rules, turno.today)
        turno.logger.event(
            "decision",
            message_id=turno.inbound.message_id,
            stage=state.stage.value,
            actions=[a.kind for a in actions],
        )
        for action in actions:
            state = await self._executar(state, action, turno, rodada)
        return await self._resolver_cep_pendente(state, turno, rodada)

    async def _resolver_cep_pendente(self, state: LeadState, turno: _Turno, rodada: int) -> LeadState:
        """CONFIRMA_CEP sem `cep_info` = a policy está esperando o ViaCEP. Busca e redecide."""
        if state.stage is not Stage.CONFIRMA_CEP or state.cep_info is not None or not state.cep:
            return state
        if rodada >= MAX_RODADAS:
            return await self._cortar_rodadas(state, turno, "cep_sem_resolucao")
        if not config_store.param("tools.viacep.enabled"):
            # Toggle do Studio: sem consulta externa. `existe=None` é o mesmo sinal de
            # "ViaCEP fora do ar", que a policy já sabe tratar (aceita sem confirmar).
            state.cep_info = CepInfo(cep=state.cep, existe=None)
            turno.logger.event(
                "cep_lookup",
                message_id=turno.inbound.message_id,
                skipped=True,
                existe=None,
                cidade=None,
                uf=None,
            )
        else:
            info = await self._lookup_cep(state.cep, self._timeout_cep())
            state.cep_info = info
            turno.logger.event(
                "cep_lookup",
                message_id=turno.inbound.message_id,
                existe=info.existe,
                cidade=info.cidade,
                uf=info.uf,
            )
        return await self._decidir_e_executar(state, None, turno, rodada + 1)

    def _timeout_cep(self) -> float:
        """Timeout do ViaCEP: o injetado (testes) ou o efetivo do store (Studio)."""
        if self._cep_timeout_s is not None:
            return self._cep_timeout_s
        return float(config_store.param("tools.viacep.timeout_s"))

    # ------------------------------------------------------------------ ações
    async def _executar(self, state: LeadState, action: Action, turno: _Turno, rodada: int) -> LeadState:
        if action.kind in (
            "send_text", "confirm_cep", "ask_plan", "present", "present_many", "refuse", "handoff",
        ):
            if action.kind == "refuse":
                turno.logger.event("refusal", message_id=turno.inbound.message_id, motivo=action.motivo)
            if action.kind == "handoff":
                turno.logger.event(
                    "handoff",
                    message_id=turno.inbound.message_id,
                    reason=action.reason.value,
                    payload=action.payload,
                )
            await self._enviar(self._render(action, state), "template", turno)
            if action.kind == "handoff":
                await self._avisar_handoff(state, action, turno)
            return state

        # `answer_about` e `answer_with_tools` são `Reply`s com diretiva especial (dados do produto
        # / ferramentas do painel): o caminho é o mesmo (Responder + guard_price), muda o texto.
        if action.kind in ("ask_field", "reply", "answer_about", "answer_with_tools"):
            if action.kind == "ask_field":
                state.ultima_pergunta = action.campo
                # Pergunta seca, fora do primeiro turno: o texto do slot já é a pergunta pronta e
                # o modelo só teria como piorar (foi ele que reperguntou a idade já coletada).
                # Com `motivo` (ano-modelo, correção, vários carros) há o que explicar: vai ao LLM.
                if action.motivo is None and state.turnos > 1:
                    await self._enviar(fallback_text(action.campo), "template", turno)
                    return state
                directive = directive_for_field(action.campo, action.motivo)
            else:
                directive = action.directive
            inicio = time.perf_counter()
            texto = await self.responder.reply(directive, state, turno.inbound.text or "")
            self._logar_tool_calls(turno)
            turno.logger.event(
                "llm_call",
                message_id=turno.inbound.message_id,
                papel="responder",
                **_usage(self.responder, state.conversation_id),
                directive=directive,
                latency_ms=int((time.perf_counter() - inicio) * 1000),
            )
            await self._enviar(texto, "llm", turno)
            return state

        if action.kind == "do_quotes":
            return await self._cotar(state, action, turno, rodada)

        raise ValueError(f"ação desconhecida: {action.kind}")

    async def _avisar_handoff(self, state: LeadState, action: Any, turno: _Turno) -> None:
        """Chama o gancho de handoff DEPOIS de o lead receber o texto, e só uma vez por handoff.

        Depois porque o lead vem primeiro: se o aviso ao consultor travar, ele já foi respondido.
        Dentro de try/except porque um WhatsApp fora do ar não pode virar erro de turno.
        """
        if self._on_handoff is None:
            return
        try:
            await self._on_handoff(state, action)
        except Exception as exc:  # noqa: BLE001 — aviso é observabilidade, não o turno
            log.error("aviso de handoff falhou (%s): %s", type(exc).__name__, str(exc)[:200])
            turno.logger.event(
                "handoff_notice",
                message_id=turno.inbound.message_id,
                canal="notifier",
                status="erro",
                erro=f"{type(exc).__name__}: {exc}"[:200],
            )

    def _logar_tool_calls(self, turno: _Turno) -> None:
        """Grava as tools que o Responder chamou durante a resposta (antes do `llm_call`: elas
        aconteceram DENTRO dela). Responder sem tools — ou dublê de teste — não expõe o método.
        """
        drenar = getattr(self.responder, "drenar_tool_calls", None)
        if drenar is None:
            return
        for evento in drenar(turno.inbound.conversation_id):
            turno.logger.event("tool_call", message_id=turno.inbound.message_id, **evento)

    async def _cotar(self, state: LeadState, action: Any, turno: _Turno, rodada: int) -> LeadState:
        """Uma chamada por carro, em paralelo, e UMA redecisão com todos os resultados.

        Em paralelo porque a API é lenta de propósito: dois carros em série dobrariam a espera
        do lead. O aviso de "só um instante" sai no máximo uma vez, por mais carros que sejam.
        """
        # As regras que geraram este `DoQuotes` podem ter sido calculadas há vários turnos;
        # o preço sai daqui, então o catálogo é relido imediatamente antes da chamada.
        await self._atualizar_rules(turno)
        avisado = False

        async def on_slow() -> None:
            nonlocal avisado
            if avisado:
                return
            avisado = True
            texto = config_store.text("conversation.texto_lento")
            await self._enviar(self._render(SendText(text=texto), state), "template", turno)

        resultados = await asyncio.gather(
            *(self.quote_client.quote(req, on_slow) for req in action.requests)
        )
        for indice, result in enumerate(resultados):
            veiculo = state.veiculos[indice] if indice < len(state.veiculos) else None
            for attempt in result.attempts:
                turno.logger.event(
                    "quote_attempt",
                    message_id=turno.inbound.message_id,
                    quote_id=result.quote_id,
                    **attempt.model_dump(mode="json"),
                )
            turno.logger.event(
                "quote_result",
                message_id=turno.inbound.message_id,
                quote_id=result.quote_id,
                outcome=result.outcome.value,
                motivo_recusa=result.motivo_recusa,
                erro=result.erro,
                total_ms=result.total_ms,
                veiculo=veiculo.rotulo() if veiculo is not None else None,
            )
            if veiculo is not None:
                veiculo.quote_result = result
        # Espelho de 1 carro (`quote_result`) para quem lê o estado de fora.
        state.quote_result = state.veiculos[0].quote_result if state.veiculos else (
            resultados[0] if resultados else None
        )
        if rodada >= MAX_RODADAS:
            return await self._cortar_rodadas(state, turno, "cotacao_sem_apresentacao")
        return await self._decidir_e_executar(state, None, turno, rodada + 1)

    async def _cortar_rodadas(self, state: LeadState, turno: _Turno, onde: str) -> LeadState:
        """Teto de rodadas atingido: o lead recebe texto e um humano assume — nunca silêncio.

        O corte só acontece por bug nosso (a policy não fechou o ciclo). Antes ele devolvia o
        estado calado, e a cotação ficava no log sem nenhuma mensagem correspondente.
        """
        return await self._falhar(state, turno, RuntimeError(f"MAX_RODADAS em {onde}"))

    # ------------------------------------------------------------------ saída
    async def _enviar(self, texto: str, source: str, turno: _Turno) -> None:
        turno.saidas += 1
        out = Outbound(
            conversation_id=turno.inbound.conversation_id,
            message_id=f"{turno.inbound.message_id}-o{turno.saidas}",
            text=texto,
            in_reply_to=turno.inbound.message_id,
            source=source,  # type: ignore[arg-type]
        )
        await turno.emit(out)
        turno.logger.event(
            "outbound",
            message_id=out.message_id,
            text=out.text,
            source=source,
            in_reply_to=out.in_reply_to,
        )

    async def _falhar(self, state: LeadState, turno: _Turno, exc: Exception) -> LeadState:
        """Erro inesperado: avisa o lead com texto neutro e escala. Log sem stack (PII)."""
        turno.logger.event(
            "error",
            message_id=turno.inbound.message_id,
            erro=type(exc).__name__,
            detalhe=str(exc)[:200],
        )
        state.stage = Stage.HANDOFF
        state.handoff_reason = HandoffReason.ERRO_INTERNO
        try:
            await self._enviar(config_store.text("conversation.texto_erro"), "template", turno)
            acao = Handoff(reason=HandoffReason.ERRO_INTERNO, payload=_payload_de_erro(state))
            turno.logger.event(
                "handoff",
                message_id=turno.inbound.message_id,
                reason=acao.reason.value,
                payload=acao.payload,
            )
            # Turno quebrado também é handoff: sem isto, ninguém do outro lado ficaria sabendo.
            await self._avisar_handoff(state, acao, turno)
        except Exception as envio:  # noqa: BLE001 — canal caiu; o estado já registra o handoff
            log.error("falha ao avisar o lead do erro: %s", type(envio).__name__)
        return state
