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

import logging
import re
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
    return FALLBACKS.get(state.ultima_pergunta or "", FALLBACK_PADRAO)


# --------------------------------------------------------------------------- prompts
_INTENT_EXEMPLOS: dict[Intent, str] = {
    Intent.SAUDACAO: '"oi", "bom dia", "vi o anúncio de vocês"',
    Intent.FORNECER_DADOS: '"tenho 35", "Onix 2019", "meu cep é 01310-100"',
    Intent.ESCOLHER_PLANO: '"quero o completo", "pode ser o do meio"',
    Intent.CONFIRMAR: '"sim", "isso", "correto", "pode ser"',
    Intent.NEGAR: '"não", "tá errado", "não é esse"',
    Intent.NAO_SEI: '"não sei o cep", "não lembro o ano"',
    Intent.ACEITAR: '"fechado", "pode emitir", "quero contratar"',
    Intent.RECUSAR: '"não quero mais", "deixa pra lá"',
    Intent.PEDIR_HUMANO: '"quero falar com um atendente", "me passa pra uma pessoa"',
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

    def __init__(self, model_id: str | None = None, db_path: Path | None = None) -> None:
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
        """Nunca levanta: falha de LLM vira `Extraction(intent=OUTRO)` marcada na observação."""
        try:
            run = await self._agent.arun(
                text,
                session_id=f"extract-{state.conversation_id}",
                dependencies={"state": state, "today": today},
            )
            if isinstance(run.content, Extraction):
                return run.content
            log.warning("extractor devolveu conteúdo fora do schema: %s", type(run.content).__name__)
        except Exception as exc:  # noqa: BLE001 — o turno continua sem extração
            log.warning("extractor falhou: %s", type(exc).__name__)
        return Extraction(intent=Intent.OUTRO, observacao="extracao_indisponivel")


class Responder:
    """Agent conversacional: sem output_schema, com histórico por `session_id`."""

    def __init__(self, model_id: str | None = None, db_path: Path | None = None) -> None:
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
        run = await self._agent.arun(
            inbound_text or "(sem texto)",
            session_id=state.conversation_id,
            dependencies={"state": state, "directive": directive},
        )
        texto = run.content if isinstance(run.content, str) else ""
        return guard_price(texto.strip(), state)


def _extractor_instructions(run_context: Any) -> str:
    deps = getattr(run_context, "dependencies", None) or {}
    return build_extraction_instructions(deps["state"], deps["today"])


def _responder_instructions(run_context: Any) -> str:
    deps = getattr(run_context, "dependencies", None) or {}
    return build_responder_instructions(deps["state"], deps["directive"])
