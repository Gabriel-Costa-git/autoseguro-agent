"""Adaptador do canal WhatsApp via Evolution API v2.

O webhook (`POST /webhook`) recebe `messages.upsert`, converte para `Inbound` e
despacha pro `Conversation.handle` só DEPOIS de responder 200 — a Evolution não
pode esperar o turno inteiro (LLM + API de cotação instável) sob pena de re-
entregar o webhook por timeout. Um `asyncio.Lock` por `conversation_id` garante
que duas mensagens seguidas do mesmo lead sejam processadas em sequência, nunca
concorrentes (senão a `Conversation` leria/gravaria o mesmo `LeadState` em paralelo).

`EvolutionSender` é o lado de saída: manda texto e o indicador "digitando". Erro de
envio vira log e um `False` de retorno, nunca exceção — um WhatsApp fora do ar não
pode derrubar o processamento do lead (o estado já avançou; só a entrega falhou) —
mas quem depende da entrega (o aviso ao consultor, em `handoff.py`) precisa saber.

`takeover` (opcional) é o interruptor "quem responde este lead": quando o operador
assume a conversa no Studio, a mensagem que chega vira só um evento `inbound` com
`modo="humano"` no log e o agente NÃO é chamado. Sem `takeover` (padrão), o
comportamento é exatamente o entregue.

**Freios do canal (F11).** Em 02/09 um contato real mandou "Boa noite" e recebeu 23
respostas iguais em 80 s: cada envio nosso gerava um `messages.upsert` de protocolo
(recibo, sem `fromMe`, sem texto), que virava turno, que virava resposta, que gerava
outro upsert. Três freios independentes, porque cada um sozinho ainda deixa passar:

1. `parse_webhook` descarta upsert sem CONTEÚDO — o que fechou o laço na origem.
2. Anti-loop por conversa: teto de respostas por minuto e nunca o mesmo texto duas
   vezes seguidas sem o lead ter escrito algo no meio. O que não sai vira evento
   `outbound_suprimido` no log da conversa, com o motivo.
3. Debounce: mensagens picadas ("oi" / "tudo bem?" / "quero cotar") viram um turno só.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, Request

from agent.config import settings
from agent.models import Inbound, MediaType, Outbound
from agent.observability import ConversationLogger
from agent.runtime_config import store as config_store

log = logging.getLogger("autoseguro.evolution")

_DIGITS_RE = re.compile(r"\d+")

# Chaves que a Evolution manda em `message` sem que ninguém tenha escrito nada: recibos,
# chaves de sessão, reação, edição, voto em enquete. Um upsert que só tem estas não é
# mensagem do lead — é protocolo. Foi o que virou 23 turnos no incidente.
CHAVES_DE_PROTOCOLO = frozenset({
    "protocolMessage",
    "senderKeyDistributionMessage",
    "messageContextInfo",
    "reactionMessage",
    "editedMessage",
    "pollUpdateMessage",
    "pollCreationMessage",
    "pollUpdateMessageMetadata",
})

# Janela do teto de respostas. Um minuto é o que o parâmetro diz; fica explícito aqui
# porque o teste do incidente depende dela.
JANELA_RATE_LIMIT_S = 60.0

# Conversa parada por mais que isto sai da memória do canal (locks, histórico do
# anti-loop). O `defaultdict(asyncio.Lock)` da entrega nunca soltava nada: um processo
# de meses acumulava um lock por telefone que já tinha ido embora.
TTL_CONVERSA_S = 3600.0
MAX_CONVERSAS_EM_MEMORIA = 5000
LIMPAR_A_PARTIR_DE = 64        # abaixo disso não vale varrer o mapa a cada webhook


def _only_digits(texto: str) -> str:
    return "".join(_DIGITS_RE.findall(texto))


def _media_type(message: dict[str, Any]) -> MediaType:
    """Sticker, vídeo e afins caem em `other` — o `MediaType` entregue tem cinco valores e
    a resposta é a mesma para todos ("não consigo abrir, pode me escrever?").
    """
    if "audioMessage" in message:
        return "audio"
    if "imageMessage" in message:
        return "image"
    if "documentMessage" in message:
        return "document"
    if "conversation" in message or "extendedTextMessage" in message:
        return "text"
    return "other"


def _texto(message: dict[str, Any]) -> str | None:
    if "conversation" in message:
        return message["conversation"]
    extended = message.get("extendedTextMessage")
    if isinstance(extended, dict):
        return extended.get("text")
    return None


def _desembrulhar(message: dict[str, Any]) -> dict[str, Any]:
    """`ephemeralMessage`/`viewOnceMessage` embrulham a mensagem real em `message`.

    Sem desembrulhar, uma mensagem temporária com texto pareceria protocolo (e seria
    descartada) e um embrulho vazio pareceria conteúdo (e viraria turno).
    """
    for embrulho in ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2", "documentWithCaptionMessage"):
        interno = message.get(embrulho)
        if isinstance(interno, dict):
            dentro = interno.get("message")
            return _desembrulhar(dentro) if isinstance(dentro, dict) else {}
    return message


def _so_protocolo(message: dict[str, Any]) -> bool:
    """Nada além de chaves de protocolo (ou nada mesmo) = não houve mensagem do lead."""
    return not (set(message) - CHAVES_DE_PROTOCOLO)


def parse_webhook(payload: dict[str, Any]) -> Inbound | None:
    """`None` para o que não vira turno: evento diferente, grupo, eco do próprio agente,
    sem `data` — e, desde a F11, upsert SEM CONTEÚDO (recibo/protocolo/stub de sistema).

    Áudio, imagem, documento e sticker continuam virando `Inbound` (sticker em `other`):
    são mensagens de verdade, o lead as enviou, e a resposta ("pode me escrever?") faz
    sentido para todas. Recibo de entrega não é mensagem de ninguém.
    """
    if payload.get("event") != "messages.upsert":
        return None
    data = payload.get("data")
    if not data:
        return None

    key = data.get("key") or {}
    if key.get("fromMe"):
        return None

    remote_jid = key.get("remoteJid") or ""
    if remote_jid.endswith("@g.us"):
        return None
    digits = _only_digits(remote_jid)
    if not digits:
        return None

    # `messageStubType` é evento de sistema da própria conversa (entrou no grupo, número
    # mudou, chamada perdida): a Evolution manda com `message` vazio ou de protocolo.
    if data.get("messageStubType") is not None:
        return None

    message = _desembrulhar(data.get("message") or {})
    if _so_protocolo(message):
        return None

    ts = data.get("messageTimestamp")

    return Inbound(
        conversation_id=f"wa-{digits}",
        message_id=key.get("id") or "",
        text=_texto(message),
        media_type=_media_type(message),
        sender_name=data.get("pushName"),
        origem=_origem(payload),
        ts=datetime.fromtimestamp(int(ts), tz=UTC) if ts is not None else datetime.now(UTC),
    )


def _origem(payload: dict[str, Any]) -> str:
    """`whatsapp:<instância>`: a instância do payload (uma Evolution serve várias) ou a do `.env`."""
    instancia = payload.get("instance") or settings.evolution_instance
    return f"whatsapp:{instancia}" if instancia else "whatsapp"


class EvolutionSender:
    """Lado de saída: `POST /message/sendText` e `POST /chat/sendPresence`.

    Nunca levanta; devolve `True`/`False`. O `False` existe desde a F11: o aviso ao
    consultor gravava `status="ok"` para mensagem que a Evolution tinha recusado.
    """

    def __init__(
        self,
        base_url: str,
        apikey: str,
        instance: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._apikey = apikey
        self._instance = instance
        self._client = client

    async def _post(self, path: str, body: dict[str, Any]) -> bool:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient()
        try:
            resp = await client.post(
                f"{self._base_url}{path}",
                json=body,
                headers={"apikey": self._apikey},
                timeout=10.0,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("falha ao chamar Evolution API (%s): %s", path, type(exc).__name__)
            return False
        finally:
            if owns_client:
                await client.aclose()
        return True

    async def send_text(self, number: str, text: str) -> bool:
        return await self._post(f"/message/sendText/{self._instance}", {"number": number, "text": text})

    async def typing(self, number: str) -> bool:
        return await self._post(
            f"/chat/sendPresence/{self._instance}",
            {"number": number, "presence": "composing", "delay": 1200},
        )


def _number_from_conversation_id(conversation_id: str) -> str:
    return conversation_id.removeprefix("wa-")


def _logar_modo_humano(inbound: Inbound) -> None:
    """Conversa assumida por um humano: a mensagem entra no log e para por aí."""
    ConversationLogger(settings.log_dir, inbound.conversation_id).event(
        "inbound",
        message_id=inbound.message_id,
        text=inbound.text,
        media_type=inbound.media_type,
        sender_name=inbound.sender_name,
        origem=inbound.origem,
        modo="humano",
    )


@dataclass
class _Conversa:
    """Memória de canal de UMA conversa: o que precisa sobreviver entre webhooks.

    Não é estado de negócio (isso é o `LeadState`): é sequência, ritmo e repetição —
    o que o canal precisa para não transformar protocolo em conversa.
    """

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    envios: list[float] = field(default_factory=list)     # instantes dos envios (janela de 1 min)
    ultimo_texto: str | None = None                       # último texto ENVIADO, para pegar repetição
    pendentes: list[Inbound] = field(default_factory=list)  # lote do debounce
    visto_em: float = 0.0


def build_app(
    conversation: Any,
    sender: EvolutionSender,
    takeover: Any = None,
    *,
    config: Any = None,
    clock: Callable[[], float] = time.monotonic,
    logger_factory: Callable[..., Any] | None = None,
) -> FastAPI:
    """`conversation` só precisa expor `async handle(inbound, emit)` (o Protocol de `Conversation`).

    `takeover` só precisa expor `is_humano(conversation_id) -> bool` (`agent.takeover.TakeoverStore`).
    `config` (um `ConfigStore`), `clock` e `logger_factory` existem para o teste: em produção
    valem o store global (com hot-reload), o relógio monotônico e o `ConversationLogger`.
    """
    app = FastAPI(title="AutoSeguro — webhook Evolution")
    loja = config if config is not None else config_store
    fabricar_logger = logger_factory or (lambda cid: ConversationLogger(settings.log_dir, cid))
    conversas: dict[str, _Conversa] = {}

    def _param(nome: str, padrao: float) -> float:
        try:
            valor = loja.param(f"tools.canal.{nome}")
        except Exception as exc:  # noqa: BLE001 — config torta não pode calar o canal
            log.warning("parâmetro canal.%s ilegível (%s)", nome, type(exc).__name__)
            return padrao
        return padrao if valor is None else float(valor)

    def _conversa(cid: str) -> _Conversa:
        agora = clock()
        _expirar_conversas(agora)
        conversa = conversas.get(cid)
        if conversa is None:
            conversa = conversas[cid] = _Conversa()
        conversa.visto_em = agora
        return conversa

    def _expirar_conversas(agora: float) -> None:
        """Sem isto o dicionário só cresce (era o caso do `defaultdict(asyncio.Lock)`, que
        guardava um lock por telefone para sempre).

        Conversa em voo — lock tomado ou mensagem esperando o debounce — nunca sai. Passado
        o TTL, sai por inatividade; se ainda assim o mapa estourar o teto, saem as mais
        antigas (a memória do canal é conveniência, o estado real está no `LeadState`).
        """
        if len(conversas) < LIMPAR_A_PARTIR_DE:
            return
        soltas = [
            (c.visto_em, cid) for cid, c in conversas.items()
            if not c.lock.locked() and not c.pendentes
        ]
        for visto_em, cid in soltas:
            if agora - visto_em > TTL_CONVERSA_S:
                del conversas[cid]
        if len(conversas) <= MAX_CONVERSAS_EM_MEMORIA:
            return
        sobra = len(conversas) - MAX_CONVERSAS_EM_MEMORIA
        for _, cid in sorted(c for c in soltas if c[1] in conversas)[:sobra]:
            del conversas[cid]

    async def _agregar(conversa: _Conversa, inbound: Inbound) -> Inbound | None:
        """Debounce: quem chegou por último leva o lote inteiro; os outros desistem.

        Devolve o `Inbound` a processar (textos juntos por "\\n") ou `None` quando outra
        mensagem chegou depois desta — aí é ELA que vai processar tudo.
        """
        debounce_s = _param("debounce_s", 2.0)
        conversa.pendentes.append(inbound)
        if debounce_s > 0:
            await asyncio.sleep(debounce_s)
            if conversa.pendentes and conversa.pendentes[-1] is not inbound:
                return None
        lote, conversa.pendentes = conversa.pendentes, []
        if not lote:
            return None
        return _juntar(lote)

    def _pode_enviar(conversa: _Conversa, out: Outbound) -> bool:
        """Os dois freios de saída. Suprimir é gravado: silêncio sem rastro é pior que ruído."""
        agora = clock()
        maximo = int(_param("max_respostas_por_minuto", 6))
        conversa.envios = [t for t in conversa.envios if agora - t < JANELA_RATE_LIMIT_S]
        if len(conversa.envios) >= maximo:
            _suprimir(out, "rate_limit", limite=maximo, janela_s=JANELA_RATE_LIMIT_S)
            return False
        if out.text == conversa.ultimo_texto:
            _suprimir(out, "repetido")
            return False
        conversa.envios.append(agora)
        conversa.ultimo_texto = out.text
        return True

    def _suprimir(out: Outbound, motivo: str, **extra: Any) -> None:
        log.warning("saída suprimida em %s (%s)", out.conversation_id, motivo)
        try:
            fabricar_logger(out.conversation_id).event(
                "outbound_suprimido",
                message_id=out.message_id,
                motivo=motivo,
                text=out.text,
                source=out.source,
                in_reply_to=out.in_reply_to,
                **extra,
            )
        except Exception as exc:  # noqa: BLE001 — log cheio/sem permissão não pode derrubar o turno
            log.error("falha ao registrar outbound_suprimido (%s)", type(exc).__name__)

    async def _processar(inbound: Inbound) -> None:
        conversa = _conversa(inbound.conversation_id)
        agregado = await _agregar(conversa, inbound)
        if agregado is None:
            return
        number = _number_from_conversation_id(agregado.conversation_id)

        async def emit(out: Outbound) -> None:
            if not _pode_enviar(conversa, out):
                return
            await sender.typing(number)
            await sender.send_text(number, out.text)

        async with conversa.lock:
            # A checagem fica DENTRO do lock: assumir/devolver no meio de um turno em voo
            # não pode fazer o agente e o operador responderem a mesma mensagem.
            if takeover is not None and takeover.is_humano(agregado.conversation_id):
                _logar_modo_humano(agregado)
                return
            if (agregado.text or "").strip():
                # O lead escreveu: o que vier agora é resposta a ISSO, não repetição.
                conversa.ultimo_texto = None
            # "Digitando" ao ENTRAR no turno, não só antes de cada saída: entre a mensagem
            # do lead e a primeira resposta cabem a extração, a policy e uma cotação lenta.
            await sender.typing(number)
            await conversation.handle(agregado, emit)

    @app.post("/webhook")
    async def webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, bool]:
        payload = await request.json()
        inbound = parse_webhook(payload)
        if inbound is None:
            return {"ignored": True}
        background_tasks.add_task(_processar, inbound)
        return {"ok": True}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # Memória do canal exposta para inspeção (teste e depuração de vazamento); ninguém
    # de fora escreve nela.
    app.state.conversas = conversas
    return app


def _juntar(lote: list[Inbound]) -> Inbound:
    """Um `Inbound` só a partir das mensagens picadas do mesmo lead.

    Identidade da ÚLTIMA (é a mais recente, e é a ela que a resposta responde) com os
    textos na ordem em que chegaram. Sem texto nenhum, o lote vale pela primeira mídia.
    """
    if len(lote) == 1:
        return lote[0]
    textos = [t for t in ((i.text or "").strip() for i in lote) if t]
    ultimo = lote[-1]
    com_texto = [i for i in lote if (i.text or "").strip()]
    return ultimo.model_copy(
        update={
            "text": "\n".join(textos) or None,
            "media_type": "text" if textos else lote[0].media_type,
            "sender_name": next((i.sender_name for i in lote if i.sender_name), None),
            "message_id": (com_texto[-1] if com_texto else ultimo).message_id,
        }
    )
