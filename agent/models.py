"""
Contratos compartilhados do agente AutoSeguro.

Este módulo é o ponto de acoplamento entre as camadas. Regras:
- Editado só pelo orquestrador este arquivo. Executores pedem mudança no reporte.
- Tudo aqui é dado puro (Pydantic/Enum). Sem I/O, sem LLM, sem HTTP.
- O LLM só produz `Extraction`. Quem decide é `policy.next_action`, que
  devolve `Action`s. Preço só existe dentro de `Quote`, que só nasce da API.

Assinaturas esperadas (implementadas nos módulos indicados):

  quote_client.QuoteClient(base_url, timeout_s=3.5, max_attempts=4, budget_s=15.0,
                           transport=None, sleep=asyncio.sleep, clock=time.monotonic)
      async get_planos() -> dict                 # cacheado após o 1º sucesso
      async quote(req: QuoteRequest, on_slow: Callable[[], Awaitable[None]] | None = None) -> QuoteResult

  rules.Rules.from_planos(planos: dict, today: date) -> Rules
      validate_idade(idade) -> Violation | None
      validate_veiculo_ano(ano) -> Violation | None
      validate_data_inicio(d: date) -> Violation | None
      normalize_cep(texto: str) -> str | None    # 8 dígitos ou None
      validate_request(req: QuoteRequest) -> list[Violation]
      planos_resumo() -> list[PlanoResumo]

  cep.lookup_cep(cep8: str, timeout_s=2.0, client: httpx.AsyncClient | None = None) -> CepInfo

  policy.next_action(state: LeadState, extraction: Extraction | None, rules: Rules,
                     today: date) -> tuple[LeadState, list[Action]]   # função pura

  presenter.render(action: Action, state: LeadState) -> str           # só ações de template
      (Present e PresentMany renderizam cotação; DoQuotes é do quote_client, não do presenter)

  pii.mask_text(texto: str) -> str
  pii.mask_obj(obj: Any) -> Any                                        # recursivo em dict/list/str

  observability.ConversationLogger(log_dir: Path, conversation_id: str)
      event(event: EventKind, message_id: str | None = None, quote_id: str | None = None, **data) -> None

  brain.Extractor.extract(text: str, state: LeadState, today: date) -> Extraction   # async
  brain.Responder.reply(directive: str, state: LeadState, inbound_text: str) -> str  # async
      (ambos implementam os Protocols LLMExtractor / LLMResponder abaixo; FakeLLM nos testes)

  conversation.Conversation(rules, quote_client, extractor, responder, log_dir, store, today=date.today)
      async handle(inbound: Inbound, emit: Callable[[Outbound], Awaitable[None]]) -> LeadState
"""
from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, Field

PlanoId = Literal["essencial", "completo", "premium"]
CampoColeta = Literal["idade", "veiculo", "cep", "plano", "data_inicio"]


# --------------------------------------------------------------------------- LLM → código
class Intent(StrEnum):
    SAUDACAO = "saudacao"
    FORNECER_DADOS = "fornecer_dados"
    ESCOLHER_PLANO = "escolher_plano"
    CONFIRMAR = "confirmar"          # "sim", "isso", "correto"
    NEGAR = "negar"                  # "não", "tá errado"
    NAO_SEI = "nao_sei"              # "não sei o cep"
    ACEITAR = "aceitar"              # quer fechar a cotação apresentada
    RECUSAR = "recusar"              # não quer seguir
    PEDIR_HUMANO = "pedir_humano"
    OBJECAO_PRECO = "objecao_preco"  # "tá caro", "vi mais barato"
    PEDIR_DESCONTO = "pedir_desconto"  # pede desconto/condição explicitamente → humano
    CONSULTA = "consulta"              # pergunta que uma tool do painel responde (só existe se houver tool)
    DUVIDA_PRODUTO = "duvida_produto"  # pergunta sobre plano, cobertura, franquia, carência, preço
    FORA_DE_ESCOPO = "fora_de_escopo"  # sinistro, outro produto, assunto que precisa de humano
    OUTRO = "outro"


class VeiculoExtraido(BaseModel):
    """Um carro citado na mensagem. O lead pode cotar mais de um de uma vez."""

    texto: str | None = None              # como o lead falou ("Onix 2022")
    ano: int | None = None
    ano_parece_modelo: bool = False       # ano > ano corrente → provável ano-modelo


class Extraction(BaseModel):
    """Saída estruturada do Extractor. Só o que a mensagem ATUAL diz; None = não citado."""

    intent: Intent = Intent.OUTRO
    idade: int | None = None
    veiculos: list[VeiculoExtraido] = Field(default_factory=list)   # TODOS os carros da mensagem
    # Compatibilidade: sempre o PRIMEIRO carro de `veiculos` (o Extractor preenche os dois).
    veiculo_texto: str | None = None      # como o lead falou ("Onix 2019")
    veiculo_ano: int | None = None
    ano_parece_modelo: bool = False       # ano > ano corrente → provável ano-modelo
    cep: str | None = None                # como o lead escreveu; rules.normalize_cep limpa
    plano_id: PlanoId | None = None
    data_inicio: date | None = None       # já resolvida para data (prompt informa a data de hoje)
    data_vaga: bool = False               # "quanto antes", "só olhando" → policy usa hoje
    observacao: str | None = None         # qualquer coisa que a policy deva saber (curto)
    indisponivel: bool = False            # LLM falhou (cota/rede/parse): policy pede para repetir, não re-pergunta


# --------------------------------------------------------------------------- API de cotação
class QuoteRequest(BaseModel):
    plano_id: PlanoId
    idade: int
    veiculo_ano: int
    cep: str | None = None                # 8 dígitos, sem hífen
    data_inicio: str                      # YYYY-MM-DD


class ProRata(BaseModel):
    dias_no_mes: int
    dias_cobrados: int
    valor_primeiro_pagamento: float


class Quote(BaseModel):
    """Espelho tipado do 200 da /quote. Única fonte de preço do sistema."""

    plano_id: str
    plano_nome: str
    premio_mensal: float
    franquia: float
    coberturas: list[str]
    multiplicadores: dict[str, float]
    carencia_coberturas: list[str]
    carencia_dias: int
    carencia_observacao: str | None = None
    moeda: str = "BRL"
    pro_rata: ProRata | None = None


AttemptStatus = Literal["ok", "recusa", "bug", "http_5xx", "timeout", "erro_rede"]


class QuoteAttempt(BaseModel):
    attempt: int                          # 1..max_attempts
    status: AttemptStatus
    http_status: int | None = None
    latency_ms: int
    error: str | None = None              # curto, sem PII


class QuoteOutcome(StrEnum):
    OK = "ok"
    RECUSA = "recusa"                     # 422 cotacao_recusada — regra de negócio, sem retry
    BUG = "bug"                           # 422 detail / 400 payload_invalido — erro nosso, sem retry
    INDISPONIVEL = "indisponivel"         # esgotou tentativas/orçamento em 5xx/timeout/rede


class QuoteResult(BaseModel):
    quote_id: str
    outcome: QuoteOutcome
    request: QuoteRequest
    quote: Quote | None = None
    motivo_recusa: str | None = None      # texto da API em RECUSA
    erro: str | None = None               # resumo técnico em BUG/INDISPONIVEL
    attempts: list[QuoteAttempt] = Field(default_factory=list)
    total_ms: int = 0


# --------------------------------------------------------------------------- regras locais
class Violation(BaseModel):
    campo: Literal["idade", "veiculo_ano", "cep", "data_inicio"]
    tipo: Literal["fora_da_faixa", "formato", "futuro", "passado"]
    motivo: str                           # frase curta, já em linguagem de lead


class PlanoResumo(BaseModel):
    id: PlanoId
    nome: str
    franquia: float
    coberturas: list[str]


class CepInfo(BaseModel):
    cep: str                              # 8 dígitos
    existe: bool | None = None            # None = ViaCEP indisponível/timeout
    cidade: str | None = None
    uf: str | None = None


# --------------------------------------------------------------------------- estado
class Stage(StrEnum):
    INICIO = "inicio"
    COLETA_IDADE = "coleta_idade"
    COLETA_VEICULO = "coleta_veiculo"
    COLETA_CEP = "coleta_cep"
    CONFIRMA_CEP = "confirma_cep"
    ESCOLHA_PLANO = "escolha_plano"
    COTANDO = "cotando"
    APRESENTADO = "apresentado"
    ENCERRADO_RECUSA = "encerrado_recusa"
    HANDOFF = "handoff"
    ENCERRADO = "encerrado"


class HandoffReason(StrEnum):
    LEAD_ACEITOU = "lead_aceitou"                 # quer fechar → humano emite
    LEAD_PEDIU_HUMANO = "lead_pediu_humano"
    COTACAO_INDISPONIVEL = "cotacao_indisponivel"  # API esgotou tentativas
    ERRO_INTERNO = "erro_interno"                 # 400/422 detail — bug nosso
    FORA_DE_ESCOPO = "fora_de_escopo"
    NEGOCIACAO = "negociacao"                     # insiste em desconto/condição
    SEM_PROGRESSO = "sem_progresso"               # N turnos sem avançar a coleta


class VeiculoColetado(BaseModel):
    """Um carro do lead e a cotação dele. Fonte da verdade de `LeadState.veiculos`."""

    texto: str | None = None
    ano: int | None = None
    quote_result: QuoteResult | None = None

    def rotulo(self) -> str:
        """Como o carro é citado nos textos: as palavras do próprio lead, com o ano se faltar."""
        texto = (self.texto or "").strip()
        if not texto:
            return f"carro {self.ano}" if self.ano else "carro"
        if self.ano and str(self.ano) not in texto:
            return f"{texto} {self.ano}"
        return texto


class LeadState(BaseModel):
    conversation_id: str
    stage: Stage = Stage.INICIO
    lead_nome: str | None = None
    idade: int | None = None
    veiculos: list[VeiculoColetado] = Field(default_factory=list)   # fonte da verdade
    # Espelho de `veiculos[0]` (sincronizado só em `policy._absorver`): mantém 1 carro idêntico
    # ao entregue para quem lê o estado de fora (resumo do prompt, handoff, Lab).
    veiculo_texto: str | None = None
    veiculo_ano: int | None = None
    cep: str | None = None                # 8 dígitos
    cep_info: CepInfo | None = None
    cep_confirmado: bool = False
    cep_tentativas: int = 0
    cep_ausente: bool = False             # lead não sabe → cota sem CEP, avisar "pode subir"
    plano_id: PlanoId | None = None
    plano_perguntado: bool = False        # a pergunta do plano já foi feita (não se repete)
    plano_assumido: bool = False          # o lead não escolheu; a policy assumiu o padrão
    data_inicio: date | None = None       # None → hoje na hora de cotar
    quote_result: QuoteResult | None = None   # espelho de `veiculos[0].quote_result`
    handoff_reason: HandoffReason | None = None
    recusa_campo: str | None = None       # campo que causou a recusa ("idade"/"veiculo_ano"): permite reabrir
    turnos: int = 0
    turnos_sem_progresso: int = 0
    objecoes: int = 0
    ultima_pergunta: CampoColeta | None = None
    origem: str | None = None             # canal de entrada: "whatsapp:<instância>", "cli", "lab"


# --------------------------------------------------------------------------- ações (código → canal/LLM)
class AskField(BaseModel):
    kind: Literal["ask_field"] = "ask_field"
    campo: CampoColeta
    motivo: str | None = None             # ex.: "ano 2027 parece ano-modelo; confirme o ano de fabricação"


class ConfirmCep(BaseModel):
    kind: Literal["confirm_cep"] = "confirm_cep"
    cep: str
    cidade: str
    uf: str


class AskPlan(BaseModel):
    kind: Literal["ask_plan"] = "ask_plan"
    planos: list[PlanoResumo]


class DoQuotes(BaseModel):
    """Uma cotação por carro, mesmo plano. Com 1 carro é a lista de um elemento."""

    kind: Literal["do_quotes"] = "do_quotes"
    requests: list[QuoteRequest]


class Present(BaseModel):
    kind: Literal["present"] = "present"
    result: QuoteResult                   # outcome == OK
    cep_ausente: bool = False


class PresentMany(BaseModel):
    """Apresentação de 2+ carros numa mensagem só: um bloco por carro e UM fechamento."""

    kind: Literal["present_many"] = "present_many"
    resultados: list[VeiculoColetado]     # na ordem em que o lead citou; cada um com sua cotação
    cep_ausente: bool = False


class Refuse(BaseModel):
    kind: Literal["refuse"] = "refuse"
    motivo: str


class Handoff(BaseModel):
    kind: Literal["handoff"] = "handoff"
    reason: HandoffReason
    payload: dict[str, Any] = Field(default_factory=dict)   # dados coletados + cotação + ref do log


class Reply(BaseModel):
    """Resposta livre do LLM guiada por diretiva. Nunca contém valores."""

    kind: Literal["reply"] = "reply"
    directive: str


class SendText(BaseModel):
    """Texto determinístico já pronto (ex.: 'só um instante', 'não consigo ouvir áudio')."""

    kind: Literal["send_text"] = "send_text"
    text: str


class AnswerAbout(BaseModel):
    """Resposta a uma dúvida sobre o PRODUTO, com os dados reais dos planos na diretiva.

    Não muda a etapa da venda: o agente responde e retoma a coleta na mesma mensagem.
    """

    kind: Literal["answer_about"] = "answer_about"
    directive: str


class AnswerWithTools(BaseModel):
    """Como `Reply`, mas o turno é sobre uma pergunta que uma tool do painel responde.

    A policy só a emite quando há tool habilitada (`Intent.CONSULTA`); quem decide se chama
    alguma — e qual — é o modelo, com as `Function`s que o Responder já carrega.
    """

    kind: Literal["answer_with_tools"] = "answer_with_tools"
    directive: str


Action = Annotated[
    AskField | ConfirmCep | AskPlan | DoQuotes | Present | PresentMany | Refuse | Handoff | Reply
    | SendText | AnswerAbout | AnswerWithTools,
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------- canal
MediaType = Literal["text", "audio", "image", "document", "other"]


class Inbound(BaseModel):
    conversation_id: str
    message_id: str
    text: str | None = None
    media_type: MediaType = "text"
    sender_name: str | None = None
    origem: str | None = None             # quem preenche é o canal; o turno copia para o `LeadState`
    ts: datetime = Field(default_factory=datetime.now)


class Outbound(BaseModel):
    conversation_id: str
    message_id: str
    text: str
    in_reply_to: str | None = None
    source: Literal["template", "llm"] = "template"
    ts: datetime = Field(default_factory=datetime.now)


# --------------------------------------------------------------------------- observabilidade
EventKind = Literal[
    "inbound", "outbound", "extraction", "decision",
    "quote_attempt", "quote_result", "cep_lookup",
    "llm_call", "tool_call", "handoff", "handoff_notice", "refusal", "error",
    # F11 — o que o log escondia: retry/erro do LLM, guardrail que apagou preço, refresh
    # do /planos, saída que o canal NÃO entregou, takeover devolvido por inatividade e
    # campo que veio da regex de fallback em vez do modelo.
    "llm_retry", "llm_error", "llm_guard", "planos_refresh",
    "outbound_suprimido", "takeover_expirado", "extraction_regex",
]


# --------------------------------------------------------------------------- protocolos (para FakeLLM)
class LLMExtractor(Protocol):
    async def extract(self, text: str, state: LeadState, today: date) -> Extraction: ...


class LLMResponder(Protocol):
    async def reply(self, directive: str, state: LeadState, inbound_text: str) -> str: ...


class StateStore(Protocol):
    def get(self, conversation_id: str) -> LeadState | None: ...
    def put(self, state: LeadState) -> None: ...
