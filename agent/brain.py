"""Camada LLM do agente: `Extractor` (mensagem → `Extraction`) e `Responder` (diretiva → texto).

O LLM faz só duas coisas: extrair dados e falar. Ele não decide nada (isso é da
`policy`) e nunca vê preço — o estado enviado no prompt é resumido sem valores e
`guard_price` é a última linha de defesa da regra de ouro: preço só sai da API,
renderizado pelo `presenter`.

Os imports do agno são feitos dentro dos construtores de propósito: as funções
puras deste módulo (prompts e guardrail) são testadas sem carregar o SDK nem
exigir chave de API.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any

from agent.config import settings
from agent.models import CampoColeta, Extraction, Intent, LeadState, QuoteOutcome

log = logging.getLogger("autoseguro.brain")

# --------------------------------------------------------------------------- guardrail
# Só dinheiro: "R$", "209,90" e "reais". Percentual fica fora porque o lead pode
# falar de franquia/desconto sem que a resposta cite valor — o prompt já proíbe.
_PRECO_RE = re.compile(r"R\$|\d+,\d{2}|\bre[aá]is\b", re.IGNORECASE)

FALLBACKS: dict[str, str] = {
    "idade": "Pra te cotar direitinho: quantos anos você tem?",
    "veiculo": "Qual o modelo e o ano de fabricação do carro?",
    "cep": "Qual o CEP de onde o carro dorme à noite?",
    "plano": "Qual dos planos você quer que eu cote?",
    "data_inicio": "A partir de quando você quer o seguro valendo?",
}
FALLBACK_PADRAO = (
    "Valor eu só passo depois que o sistema cotar, pra não te falar bobagem. "
    "Podemos seguir com os dados?"
)


def contem_preco(texto: str) -> bool:
    """True se o texto tem cara de valor em dinheiro (R$, 1.234,56, 'reais')."""
    return bool(_PRECO_RE.search(texto or ""))


def guard_price(text: str, state: LeadState) -> str:
    """Substitui a resposta do LLM por um fallback determinístico se ela citar dinheiro.

    Só libera valor quando ele veio da API (`quote_result.outcome == OK`); nesse caso
    o texto com preço é o do `presenter`, não do LLM, e o Responder está apenas
    conversando em cima de uma cotação já apresentada.
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


# --------------------------------------------------------------------------- resiliência do LLM
# O provedor de LLM é a MESMA classe de dependência instável que a `/quote`: a cota
# gratuita do Gemini é 5 req/min e cada turno gasta 2 chamadas. Fato verificado no
# agno 3.0.5 (`agno/agent/_run.py`): `Agent.retries` é 0 por padrão, então o SDK NÃO
# re-tenta; e `arun` NÃO levanta — ele captura a exceção, marca
# `run.status = RunStatus.error` e coloca `str(exc)` em `run.content`. Por isso aqui
# se trata tanto a exceção quanto o run marcado como erro.
MAX_TENTATIVAS_LLM = 4          # 1 chamada + 3 novas tentativas
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


def _fallback(state: LeadState) -> str:
    """Texto determinístico para o campo pendente — nunca deixa o turno sem resposta."""
    return FALLBACKS.get(state.ultima_pergunta or "", FALLBACK_PADRAO)


# --------------------------------------------------------------------------- prompts
_INTENT_EXEMPLOS: dict[Intent, str] = {
    Intent.SAUDACAO: '"oi", "bom dia", "vi o anúncio de vocês"',
    Intent.FORNECER_DADOS: '"tenho 35", "Onix 2019", "meu cep é 01310-100"',
    Intent.ESCOLHER_PLANO: '"quero o completo", "pode ser o do meio"',
    Intent.CONFIRMAR: '"sim", "isso", "correto", "pode ser"',
    Intent.NEGAR: '"não", "tá errado", "não é esse"',
    Intent.NAO_SEI: '"não sei o cep", "não lembro o ano"',
    Intent.ACEITAR: '"fechado", "pode emitir", "quero contratar", "pode passar pro consultor fechar" (depois da cotação, concordar em seguir é ACEITAR)',
    Intent.RECUSAR: '"não quero mais", "deixa pra lá"',
    Intent.PEDIR_HUMANO: '"quero falar com um atendente", "me passa pra uma pessoa" (só quando ele quer um humano EM VEZ do bot, não para fechar a cotação)',
    Intent.OBJECAO_PRECO: '"tá caro", "vi mais barato", "achei salgado"',
    Intent.PEDIR_DESCONTO: '"tem desconto?", "consegue baixar?", "faz por menos?"',
    Intent.FORA_DE_ESCOPO: '"bati o carro", "quero ver minha apólice", "seguro de vida"',
    Intent.OUTRO: "qualquer coisa que não se encaixe nas anteriores",
}

_CAMPO_DIRETIVA: dict[str, str] = {
    "idade": "pergunte a idade do condutor principal",
    "veiculo": "pergunte o modelo e o ano de fabricação do carro",
    "cep": "pergunte o CEP de onde o carro dorme à noite",
    "plano": "pergunte qual plano ele quer cotar",
    "data_inicio": "pergunte a partir de quando ele quer o seguro valendo",
}


def directive_for_field(campo: CampoColeta, motivo: str | None = None) -> str:
    """Traduz um `AskField` da policy na diretiva em linguagem natural do Responder."""
    base = _CAMPO_DIRETIVA.get(campo, f"pergunte {campo}")
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
    """Prompt do Extractor: papel, data de hoje, estado, última pergunta e regras."""
    intents = "\n".join(f"- {i.value}: {ex}" for i, ex in _INTENT_EXEMPLOS.items())
    ultima = state.ultima_pergunta or "nenhuma"
    return f"""Você extrai dados estruturados de UMA mensagem de um lead de seguro auto no WhatsApp (pt-BR).
Você não conversa e não decide nada: só preenche o schema.

Hoje é {today.isoformat()} (ano corrente: {today.year}).
Já coletado até aqui: {resumo_state(state)}.
Última pergunta que o consultor fez: {ultima}. Use isso para desambiguar respostas curtas
("sim" responde a essa pergunta; "35" é idade se a pergunta foi idade; "2019" é ano do carro se a pergunta foi o veículo).

Regras:
- Extraia SÓ o que a mensagem ATUAL diz. O que já estava coletado não se repete: campo não citado agora = null.
- idade: número inteiro de anos do condutor. Não confunda com ano do carro.
- veiculo_texto: como o lead falou ("Onix 2019", "gol quadrado"). veiculo_ano: o ano citado.
- ano_parece_modelo = true quando veiculo_ano for maior que {today.year} (provável ano-modelo, não de fabricação).
- cep: copie como o lead escreveu, sem limpar.
- plano_id: só se ele nomear um plano (essencial, completo, premium).
- data_inicio: resolva datas relativas para uma data real usando hoje = {today.isoformat()}
  ("mês que vem" = dia 1 do mês seguinte; "dia 15" = dia 15 do mês corrente, ou do próximo se já passou).
- data_vaga = true (e data_inicio null) para "quanto antes", "o mais rápido possível", "só estou olhando".
- observacao: no máximo uma frase curta com algo que o vendedor precise saber. Nunca invente.
- NUNCA invente preço, valor, desconto ou cobertura. Você não tem essa informação.

intent (escolha exatamente um):
{intents}"""


def build_responder_instructions(state: LeadState, directive: str) -> str:
    """Prompt do Responder: persona + estado (sem valores) + diretiva do turno + regras duras."""
    return f"""Você é consultor de vendas da AutoSeguro falando por WhatsApp, em pt-BR.
Tom: humano, direto, cordial, frases curtas. UMA pergunta por mensagem. No máximo um emoji, e só quando couber.
Nada de markdown, listas ou textão. Você já está no meio da conversa: não se reapresente a cada mensagem.

Estado da conversa: {resumo_state(state)}.

SUA TAREFA NESTE TURNO: {directive}
Responda à última mensagem do lead e cumpra essa tarefa. Não faça mais nada além disso.

Regras invioláveis:
- NUNCA cite preço, valor, mensalidade, franquia em reais, percentual, desconto ou multiplicador.
  Quem passa valor é o sistema de cotação, em outra mensagem. Se o lead perguntar o preço antes da cotação,
  diga que precisa dos dados para cotar e siga com a tarefa do turno.
- NUNCA prometa desconto, condição especial, brinde ou prazo de pagamento.
- NUNCA peça CPF, e-mail, telefone, placa, RG, endereço completo ou dados bancários.
- Não invente cobertura, carência nem regra de aceitação. Não repita dados que o lead não deu.
- Se não souber, diga que vai confirmar com o time."""


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


class Extractor:
    """Agent com `output_schema=Extraction`, sem histórico e sem tools.

    Sem histórico de propósito: cada mensagem é analisada isolada, e todo o contexto
    necessário (estado + última pergunta) já vai no prompt — assim a extração é
    reprodutível e não se contamina com turnos antigos.
    """

    def __init__(
        self,
        model_id: str | None = None,
        db_path: Path | None = None,
        *,
        agent: Any | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sleep = sleep
        if agent is not None:      # dublê nos testes: não carrega o SDK nem exige chave
            self._agent = agent
            return

        from agno.agent import Agent
        from agno.db.sqlite import SqliteDb
        from agno.models.google import Gemini

        db_file = Path(db_path or settings.agent_db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        self._agent = Agent(
            name="autoseguro-extractor",
            model=Gemini(
                id=model_id or settings.gemini_model,
                api_key=settings.google_api_key,
                temperature=0.0,
            ),
            db=SqliteDb(db_file=str(db_file)),
            output_schema=Extraction,
            instructions=_extractor_instructions,
            add_history_to_context=False,
            markdown=False,
            telemetry=False,
        )

    async def extract(self, text: str, state: LeadState, today: date) -> Extraction:
        """Nunca levanta. Cota estourada / 5xx re-tenta; esgotou, marca `indisponivel`."""

        async def chamada() -> Extraction:
            run = await self._agent.arun(
                text,
                session_id=f"extract-{state.conversation_id}",
                dependencies={"state": state, "today": today},
            )
            erro = _erro_do_run(run)
            if erro is not None:
                raise ChamadaLLMFalhou(erro)
            if isinstance(run.content, Extraction):
                return run.content
            # Sem status de erro e fora do schema: o modelo respondeu outra coisa.
            # Não é transitório — re-tentar só queimaria cota.
            raise ChamadaLLMFalhou(f"conteúdo fora do schema: {type(run.content).__name__}", status_code=422)

        try:
            return await _com_retry(chamada, sleep=self._sleep, papel="extractor")
        except Exception as exc:  # noqa: BLE001 — o turno continua sem extração
            log.warning("extractor indisponível (%s): %s", type(exc).__name__, str(exc)[:200])
        return Extraction(intent=Intent.OUTRO, indisponivel=True, observacao="extracao_indisponivel")


class Responder:
    """Agent conversacional: sem output_schema, com histórico por `session_id`."""

    def __init__(
        self,
        model_id: str | None = None,
        db_path: Path | None = None,
        *,
        agent: Any | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sleep = sleep
        if agent is not None:      # dublê nos testes
            self._agent = agent
            return

        from agno.agent import Agent
        from agno.db.sqlite import SqliteDb
        from agno.models.google import Gemini

        db_file = Path(db_path or settings.agent_db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        self._agent = Agent(
            name="autoseguro-responder",
            model=Gemini(
                id=model_id or settings.gemini_model,
                api_key=settings.google_api_key,
                temperature=0.4,
            ),
            db=SqliteDb(db_file=str(db_file)),
            instructions=_responder_instructions,
            post_hooks=[_price_guard_hook],
            add_history_to_context=True,
            num_history_runs=8,
            markdown=False,
            telemetry=False,
        )

    async def reply(self, directive: str, state: LeadState, inbound_text: str) -> str:
        """Nunca devolve vazio: LLM fora do ar cai no fallback determinístico do campo."""

        async def chamada() -> str:
            run = await self._agent.arun(
                inbound_text or "(sem texto)",
                session_id=state.conversation_id,
                dependencies={"state": state, "directive": directive},
            )
            erro = _erro_do_run(run)
            if erro is not None:
                raise ChamadaLLMFalhou(erro)
            texto = run.content.strip() if isinstance(run.content, str) else ""
            if not texto:
                raise ChamadaLLMFalhou("resposta vazia do modelo", status_code=422)
            return texto

        try:
            texto = await _com_retry(chamada, sleep=self._sleep, papel="responder")
        except Exception as exc:  # noqa: BLE001 — o lead não pode ficar sem resposta
            log.warning("responder indisponível (%s): %s", type(exc).__name__, str(exc)[:200])
            return _fallback(state)
        return guard_price(texto, state)


def _extractor_instructions(run_context: Any) -> str:
    deps = getattr(run_context, "dependencies", None) or {}
    return build_extraction_instructions(deps["state"], deps["today"])


def _responder_instructions(run_context: Any) -> str:
    deps = getattr(run_context, "dependencies", None) or {}
    return build_responder_instructions(deps["state"], deps["directive"])
